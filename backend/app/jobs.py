from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert

from .db import Base, Cabinet, Job, LabelBatch, LabelPrint, Order, PrintTask, Session, engine

OZON_ACCOUNTS = ("1", "2")
WB_ACCOUNT = "w"
ACTIVE = ("pending", "leased")
OZON_STATUSES = ("awaiting_packaging", "awaiting_deliver")
WB_BOARD_STATUSES = ("wb_new", "wb_confirm")
WB_STATUSES = (*WB_BOARD_STATUSES, "wb_complete")
LABEL_STATUSES = ("awaiting_deliver", "wb_confirm")
PDF_LIMIT = 8 * 1024 * 1024
ERRORS = {
    "access": "{marketplace} отклонил доступ. Проверьте ключ и его права.",
    "rate": "{marketplace} ограничил частоту запросов.",
    "unavailable": "Связь с {marketplace} временно недоступна.",
    "response": "Получен неожиданный ответ {marketplace}.",
    "label": "{marketplace} не смог подготовить этикетку.",
    "profile": "На ПК не настроен ключ этого кабинета.",
}


def now():
    return datetime.now(UTC)


def initialize():
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(73608201)"))
        # Начальная схема v1. Последующие версии добавляют миграции здесь.
        Base.metadata.create_all(connection)
        for key in OZON_ACCOUNTS:
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
        for key in db.scalars(select(Cabinet.key)):
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


def claim(db, account_keys):
    instant = now()
    query = select(Job).where(
        Job.account_key.in_(account_keys),
        or_(
            (Job.state == "pending") & (Job.available_at <= instant),
            (Job.state == "leased") & (Job.lease_until <= instant),
        ),
    ).order_by((Job.kind == "label_pdf").desc(), Job.available_at, Job.id).limit(1)
    job = db.scalar(query.with_for_update(skip_locked=True))
    if job is None:
        return None
    job.state = "leased"
    job.lease_token = uuid4()
    job.attempts += 1
    job.lease_until = instant + timedelta(seconds=90)
    return job


def apply_snapshot(db, key, result):
    rows = {row.posting_number: row for row in result.orders}
    allowed = WB_STATUSES if key == WB_ACCOUNT else OZON_STATUSES
    if len(rows) != len(result.orders) or any(
        row.account_key != key or row.status not in allowed for row in rows.values()
    ):
        raise ValueError("Несогласованный снимок")
    if key == WB_ACCOUNT:
        discard_reassigned_stickers(db, rows)
    db.execute(delete(Order).where(Order.account_key == key))
    db.add_all(Order(
        account_key=key, posting_number=row.posting_number,
        status=row.status, data=row.model_dump(mode="json"),
    ) for row in rows.values())
    cabinet = db.get(Cabinet, key)
    cabinet.updated_at = now()
    cabinet.error = (
        "Wildberries отклонил доступ к названиям. Укажите токен категории «Контент» в WB_CONTENT_TOKEN файла .env.wb."
        if key == WB_ACCOUNT and result.catalog_access_denied else None
    )


def discard_reassigned_stickers(db, rows):
    prior = db.scalars(select(Order).where(
        Order.account_key == WB_ACCOUNT, Order.status.in_(("wb_confirm", "wb_complete")),
    ).order_by(Order.posting_number).with_for_update())
    changed = []
    for order in prior:
        updated = rows.get(order.posting_number)
        # WB may assign a new sticker when a task returns to assembly or changes supply.
        if updated is not None and (
            updated.supply_id != order.data.get("supply_id")
            or order.status == "wb_complete" and updated.status == "wb_confirm"
            or updated.status == "wb_new"
        ):
            changed.append(order.posting_number)
    if changed:
        filters = (Job.account_key == WB_ACCOUNT, Job.kind == "label_pdf", Job.posting_number.in_(changed))
        db.execute(update(Job).where(*filters, Job.state.in_(ACTIVE)).values(
            state="failed", lease_until=None, error="Поставка WB изменилась. Загрузите актуальную этикетку.",
        ))
        db.execute(update(Job).where(*filters, Job.state == "ready").values(label_until=now()))
        db.execute(delete(LabelPrint).where(LabelPrint.account_key == WB_ACCOUNT, LabelPrint.posting_number.in_(changed)))


def fail_job(db, job, completion):
    marketplace = "Wildberries" if job.account_key == WB_ACCOUNT else "Ozon"
    job.error = ERRORS.get(completion.error, ERRORS["response"]).format(marketplace=marketplace)
    if job.kind == "sync_orders":
        db.get(Cabinet, job.account_key).error = job.error
    if completion.status == "retryable_error" and (job.kind == "sync_orders" or job.attempts < 5):
        delay = max(completion.retry_after or 0, min(300, 5 * 2 ** min(job.attempts, 6)))
        job.available_at = now() + timedelta(seconds=delay)
        job.state = "pending"
    else:
        job.state = "failed"
    job.lease_until = None
