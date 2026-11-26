import json
import re
from datetime import timedelta
from functools import lru_cache
from hashlib import sha256
from io import BytesIO
from uuid import UUID, uuid4

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PyPdfError
from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert

from .db import Cabinet, Job, LabelBatch, LabelPrint, Order, OzonAssembly, PrintLog, PrintTask, write_transaction
from .jobs import ACTIVE, LABEL_STATUSES, OZON_ACCOUNTS, REASSIGNED_LABEL_ERROR, WB_ACCOUNT, now
from .label_title import NoTitleSpace, add_title

PRINT_LEASE = timedelta(minutes=2)
TITLE_SPACE_ERROR = "На этикетке нет места для названия. Проверьте название и повторите печать."


class BatchTitleSpace(NoTitleSpace):
    def __init__(self, job_ids):
        super().__init__(TITLE_SPACE_ERROR)
        self.job_ids = frozenset(job_ids)


def book_title(order):
    products = order.data.get("products", []) if order else []
    names = (product.get("name") or "" for product in products)
    return ", ".join(dict.fromkeys(name for name in (" ".join(value.split()) for value in names) if name))


@lru_cache(maxsize=512)
def pdf_label_number(path, account_key):
    try:
        with path.open("rb") as source:
            reader = PdfReader(source)
            if account_key == WB_ACCOUNT:
                return reader.metadata.title if reader.metadata else None
            numbers = (
                f"{prefix} {suffix}"
                for page in reader.pages
                for prefix, suffix in re.findall(r"(?mi)^\s*(ii[0-9]{7})\s+([0-9]{4})[ \t]*$", page.extract_text())
            )
            return ", ".join(dict.fromkeys(numbers)) or None
    except (OSError, PyPdfError, ValueError):
        return None


def batch_item(order, job_id, with_title):
    title = book_title(order)
    print_title = title if with_title or order.account_key in OZON_ACCOUNTS else ""
    return {"job_id": str(job_id), "title": print_title, "book_title": title,
            "account_key": order.account_key, "posting_number": order.posting_number}


def create_batch(db, account, items):
    batch_id = uuid4()
    db.add(LabelBatch(id=batch_id, account=account, labels=items, expires_at=now() + timedelta(days=1)))
    return batch_id


def queue_print(db, account, items):
    printable = [{"job_id": item["job_id"], "title": item["title"]} for item in items]
    fingerprint = sha256(json.dumps(printable, sort_keys=True).encode()).hexdigest()
    batch_id = create_batch(db, account, items)
    db.flush()
    task_id = db.scalar(insert(PrintTask).values(
        id=uuid4(), batch_id=batch_id, fingerprint=fingerprint, state="pending", created_at=now(),
    ).on_conflict_do_nothing().returning(PrintTask.id))
    if task_id is not None:
        return task_id
    db.delete(db.get(LabelBatch, batch_id))
    return db.scalar(select(PrintTask.id).where(PrintTask.fingerprint == fingerprint)
                     .order_by(PrintTask.created_at.desc()).limit(1))


def label_metadata(db, batch, label_dir):
    incomplete_ids = [UUID(item["job_id"]) for item in batch.labels
                      if not item.get("posting_number") or not item.get("book_title")]
    originals = {}
    if incomplete_ids:
        rows = db.execute(select(Job, Order).outerjoin(Order, and_(
            Order.account_key == Job.account_key, Order.posting_number == Job.posting_number,
        )).where(Job.id.in_(incomplete_ids)))
        originals = {job.id: (job, order) for job, order in rows}

    items = []
    for item in batch.labels:
        job_id = UUID(item["job_id"])
        if job_id in originals:
            job, order = originals[job_id]
            item = {**item, "account_key": job.account_key, "posting_number": job.posting_number,
                    "book_title": item.get("book_title") or item["title"] or book_title(order)}
        if item.get("posting_number") and not item.get("label_number"):
            item = {**item, "label_number": pdf_label_number(label_dir / f"{job_id}.pdf", item["account_key"])}
        items.append(item)
    return items


def record_print(db, task, batch, label_dir, submitted_at=None):
    rows = [
        {"task_id": task.id, "label_job_id": UUID(item["job_id"]),
         "event_at": submitted_at or task.created_at, "historical": submitted_at is None,
         "account_key": item["account_key"], "posting_number": item["posting_number"],
         "book_title": item["book_title"], "label_number": item.get("label_number")}
        for item in label_metadata(db, batch, label_dir) if item.get("posting_number")
    ]
    if rows:
        db.execute(insert(PrintLog).on_conflict_do_nothing(), rows)


def mark_submitted_labels(db, task_id):
    # Serialize with order synchronization so a replaced WB sticker cannot mark its successor.
    accounts = select(PrintLog.account_key).where(PrintLog.task_id == task_id)
    db.execute(select(Cabinet.key).where(Cabinet.key.in_(accounts)).order_by(Cabinet.key).with_for_update()).all()
    printed = select(Order.account_key, Order.posting_number).join(
        PrintLog, and_(PrintLog.account_key == Order.account_key, PrintLog.posting_number == Order.posting_number),
    ).join(Job, Job.id == PrintLog.label_job_id).where(
        PrintLog.task_id == task_id,
        or_(Order.status.in_(LABEL_STATUSES), select(OzonAssembly.job_id).where(
            OzonAssembly.label_job_id == PrintLog.label_job_id, OzonAssembly.print_after.is_(True),
        ).exists()),
        Job.error.is_distinct_from(REASSIGNED_LABEL_ERROR),
    ).distinct()
    db.execute(insert(LabelPrint).from_select(["account_key", "posting_number"], printed).on_conflict_do_nothing())


def backfill_print_log(label_dir):
    logged = select(PrintLog.task_id).where(PrintLog.task_id == PrintTask.id).exists()
    with write_transaction() as db:
        rows = db.execute(select(PrintTask, LabelBatch).join(LabelBatch, LabelBatch.id == PrintTask.batch_id)
                          .where(PrintTask.state == "submitted", ~logged))
        for task, batch in rows:
            record_print(db, task, batch, label_dir)


def batch_versions(db, batch, label_dir):
    ids = [UUID(item["job_id"]) for item in batch.labels]
    jobs = {job.id: job for job in db.scalars(select(Job).where(Job.id.in_(ids)))}
    ready = pending = 0
    failures = []
    for job_id in ids:
        job = jobs.get(job_id)
        if job is not None and job.state in ACTIVE:
            pending += 1
        elif (job is not None and job.state == "ready" and job.label_until > now()
              and (label_dir / f"{job_id}.pdf").is_file()):
            ready += 1
        else:
            failures.append((job.posting_number, job.error or "PDF недоступен") if job else ("", "PDF недоступен"))
    return ready, pending, failures


def batch_writer(db, batch, label_dir, originals=False):
    titled_jobs = [UUID(item["job_id"]) for item in batch.labels if item["title"]] if not originals else []
    wb_jobs = set(db.scalars(select(Job.id).where(
        Job.account_key == WB_ACCOUNT, Job.id.in_(titled_jobs),
    ))) if titled_jobs else set()
    writer = PdfWriter()
    blocked = []
    for item in batch.labels:
        source = (label_dir / f'{item["job_id"]}.pdf').read_bytes()
        if item["title"] and not originals:
            try:
                source = add_title(source, item["title"], wb=UUID(item["job_id"]) in wb_jobs)
            except NoTitleSpace:
                blocked.append(item["job_id"])
                continue
        writer.append(BytesIO(source), import_outline=False)
    if blocked:
        raise BatchTitleSpace(blocked)
    return writer


def claim_print(db, label_dir):
    instant = now()
    candidates = db.scalars(select(PrintTask).where(or_(
        PrintTask.state == "pending",
        and_(PrintTask.state == "leased", PrintTask.lease_until <= instant),
    )).order_by(PrintTask.created_at).with_for_update(skip_locked=True))
    for task in candidates:
        batch = db.get(LabelBatch, task.batch_id)
        if batch.expires_at <= instant:
            continue
        _, pending, failures = batch_versions(db, batch, label_dir)
        if failures:
            task.state = "failed"
            task.error = failures[0][1]
        elif not pending:
            task.state = "leased"
            task.lease_token = uuid4()
            task.lease_until = instant + PRINT_LEASE
            return task
    return None
