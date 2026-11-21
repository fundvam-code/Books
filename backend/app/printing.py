import json
from datetime import timedelta
from hashlib import sha256
from io import BytesIO
from uuid import UUID, uuid4

from pypdf import PdfWriter
from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert

from .db import Job, LabelBatch, PrintTask
from .jobs import ACTIVE, now
from .label_title import add_title

PRINT_LEASE = timedelta(minutes=2)


def create_batch(db, account, items):
    batch_id = uuid4()
    db.add(LabelBatch(id=batch_id, account=account, labels=items, expires_at=now() + timedelta(days=1)))
    return batch_id


def queue_print(db, account, items):
    fingerprint = sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()
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


def batch_writer(batch, label_dir, originals=False):
    writer = PdfWriter()
    for item in batch.labels:
        source = (label_dir / f'{item["job_id"]}.pdf').read_bytes()
        if item["title"] and not originals:
            source = add_title(source, item["title"])
        writer.append(BytesIO(source), import_outline=False)
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
