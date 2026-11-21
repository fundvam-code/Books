from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert

from .db import Base, Cabinet, Job, LabelBatch, Order, PrintTask, Session, engine

ACCOUNTS = ("1", "2")
ACTIVE = ("pending", "leased")
STATUSES = ("awaiting_packaging", "awaiting_deliver")
PDF_LIMIT = 8 * 1024 * 1024
ERRORS = {
    "access": "Ozon отклонил доступ. Проверьте ключ и его права.",
    "rate": "Ozon ограничил частоту запросов.",
    "unavailable": "Связь с Ozon временно недоступна.",
    "response": "Получен неожиданный ответ Ozon.",
    "label": "Ozon не смог подготовить этикетку.",
    "profile": "На ПК не настроен ключ этого кабинета.",
}


def now():
    return datetime.now(UTC)


def initialize():
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(73608201)"))
        # Начальная схема v1. Последующие версии добавляют миграции здесь.
        Base.metadata.create_all(connection)
        for key in ACCOUNTS:
            connection.execute(insert(Cabinet).values(key=key).on_conflict_do_nothing())


def enqueue(db, kind, key, posting=""):
    job_id = uuid4()
    query = insert(Job).values(
        id=job_id, kind=kind, account_key=key, posting_number=posting, available_at=now(),
    ).on_conflict_do_nothing().returning(Job.id)
    return db.scalar(query)


def maintain(label_dir):
    with Session.begin() as db:
        db.execute(update(PrintTask).where(PrintTask.state == "sending", PrintTask.lease_until <= now())
                   .values(state="unknown", lease_until=None))
        db.execute(delete(LabelBatch).where(LabelBatch.expires_at <= now()))
        for key in ACCOUNTS:
            enqueue(db, "sync_orders", key)
        db.execute(delete(Job).where(
            Job.state.in_(("done", "failed", "expired")),
            Job.available_at < now() - timedelta(days=7),
        ))
        expired = db.scalars(select(Job).where(
            Job.state == "ready", Job.label_until <= now(),
        ).with_for_update(skip_locked=True))
        for job in expired:
            (label_dir / f"{job.id}.pdf").unlink(missing_ok=True)
            job.state = "expired"


def claim(db):
    instant = now()
    job = db.scalar(select(Job).where(or_(
        (Job.state == "pending") & (Job.available_at <= instant),
        (Job.state == "leased") & (Job.lease_until <= instant),
    )).order_by((Job.kind == "label_pdf").desc(), Job.available_at, Job.id).limit(1).with_for_update(skip_locked=True))
    if job is None:
        return None
    job.state = "leased"
    job.lease_token = uuid4()
    job.attempts += 1
    job.lease_until = instant + timedelta(seconds=90)
    return job


def apply_snapshot(db, key, result):
    rows = {row.posting_number: row for row in result.orders}
    if len(rows) != len(result.orders) or any(row.account_key != key for row in rows.values()):
        raise ValueError("Несогласованный снимок")
    db.execute(delete(Order).where(Order.account_key == key))
    db.add_all(Order(
        account_key=key, posting_number=row.posting_number,
        status=row.status, data=row.model_dump(mode="json"),
    ) for row in rows.values())
    cabinet = db.get(Cabinet, key)
    cabinet.updated_at = now()
    cabinet.error = None


def fail_job(db, job, completion):
    job.error = ERRORS.get(completion.error, ERRORS["response"])
    if job.kind == "sync_orders":
        db.get(Cabinet, job.account_key).error = job.error
    if completion.status == "retryable_error" and (job.kind == "sync_orders" or job.attempts < 5):
        delay = max(completion.retry_after or 0, min(300, 5 * 2 ** min(job.attempts, 6)))
        job.available_at = now() + timedelta(seconds=delay)
        job.state = "pending"
    else:
        job.state = "failed"
    job.lease_until = None
