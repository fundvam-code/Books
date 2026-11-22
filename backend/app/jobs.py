from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, inspect, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert

from .db import (Base, Cabinet, Job, LabelBatch, LabelPrint, Order, OrderWaiting, OzonAssembly, PrintLog, PrintTask, Shift, WbAssembly, engine,
                 write_transaction)

OZON_ACCOUNTS = ("1", "2")
WB_ACCOUNT = "w"
WB_ASSEMBLY_KIND = "wb_assembly"
OZON_ASSEMBLY_KIND = "ozon_assembly"
WB_AUTO_ASSEMBLY_HEARTBEAT_ID = 3
OZON_ASSEMBLY_HEARTBEAT_BASE = 10
OZON_PRINT_HEARTBEAT_BASE = 20
ACTIVE = ("pending", "leased")
OZON_STATUSES = ("awaiting_packaging", "awaiting_deliver")
WB_BOARD_STATUSES = ("wb_new", "wb_confirm")
WB_STATUSES = (*WB_BOARD_STATUSES, "wb_complete")
LABEL_STATUSES = ("awaiting_deliver", "wb_confirm")
REASSIGNED_LABEL_ERROR = "Поставка WB изменилась. Загрузите актуальную этикетку."
PDF_LIMIT = 8 * 1024 * 1024
ERRORS = {
    "access": "{marketplace} отклонил доступ. Проверьте ключ и его права.",
    "rate": "{marketplace} ограничил частоту запросов.",
    "unavailable": "Связь с {marketplace} временно недоступна.",
    "response": "Получен неожиданный ответ {marketplace}.",
    "label": "{marketplace} не смог подготовить этикетку.",
    "profile": "На ПК не настроен ключ этого кабинета.",
    "assembly_conflict": "Проверьте, что поставка открыта, подходит для заказа и заказ ещё находится в «Новых».",
    "assembly_unknown": "Wildberries не подтвердил результат. Проверьте заказ в кабинете перед повтором.",
    "assembly_no_supply": "Открытая поставка не найдена. Создайте поставку в WB Partners и повторите перенос.",
    "assembly_multiple_supplies": "Найдено несколько открытых поставок. Выберите нужную в кабинете WB Partners.",
    "ozon_assembly_conflict": "Ozon не подтвердил сборку. Проверьте статус отправления в кабинете.",
    "ozon_assembly_unknown": "Не удалось проверить результат сборки Ozon. Проверьте отправление в кабинете перед повтором.",
}
CATALOG_ERRORS = {
    "denied": "Wildberries отклонил доступ к названиям. Проверьте токен категории «Контент» в .env.wb.",
    "unavailable": "Агент временно не получил названия из каталога Wildberries.",
    "not_found": "Для части заказов названия не найдены в каталоге Wildberries.",
}


def now():
    return datetime.now(UTC)


def initialize():
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(73608201)"))
        # Начальная схема v1. Последующие версии добавляют миграции здесь.
        Base.metadata.create_all(connection)
        columns = inspect(connection).get_columns(WbAssembly.__tablename__)
        if any(column["name"] == "supply_id" and not column["nullable"] for column in columns):
            connection.execute(text("ALTER TABLE wb_assembly ALTER COLUMN supply_id DROP NOT NULL"))
        connection.execute(insert(Shift).values(id=1, revision=uuid4()).on_conflict_do_nothing())
        for key in OZON_ACCOUNTS:
            connection.execute(insert(Cabinet).values(key=key).on_conflict_do_nothing())


def enqueue(db, kind, key, posting=""):
    job_id = uuid4()
    query = insert(Job).values(
        id=job_id, kind=kind, account_key=key, posting_number=posting, available_at=now(),
    ).on_conflict_do_nothing().returning(Job.id)
    return db.scalar(query)


def maintain(label_dir):
    with write_transaction() as db:
        instant = now()
        db.execute(update(Job).where(
            Job.kind == WB_ASSEMBLY_KIND, Job.state == "leased", Job.lease_until <= instant, assembly_started(),
        ).values(state="failed", error=ERRORS["assembly_unknown"], lease_until=None))
        ozon_started = select(OzonAssembly.job_id).where(
            OzonAssembly.job_id == Job.id, OzonAssembly.started_at.is_not(None),
        ).exists()
        db.execute(update(Job).where(
            Job.kind == OZON_ASSEMBLY_KIND, Job.state == "leased", Job.lease_until <= instant, ozon_started,
        ).values(state="failed", error=ERRORS["ozon_assembly_unknown"], lease_until=None))
        db.execute(update(PrintTask).where(PrintTask.state == "sending", PrintTask.lease_until <= instant)
                   .values(state="unknown", lease_until=None))
        printing = select(PrintTask.id).where(
            PrintTask.batch_id == LabelBatch.id, PrintTask.state.in_(("leased", "sending")),
            PrintTask.lease_until > instant,
        ).exists()
        db.execute(delete(LabelBatch).where(LabelBatch.expires_at <= instant, ~printing))
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


class ShiftConflict(Exception):
    pass


def start_shift(revision):
    with write_transaction(exclusive=True) as db:
        current = db.get(Shift, 1)
        if current.revision != revision:
            raise ShiftConflict("Смена уже была начата. Проверьте список отправлений.")
        instant = now()
        sending = select(PrintTask.id).where(PrintTask.state == "sending", PrintTask.lease_until > instant).exists()
        if db.scalar(select(sending)):
            raise ShiftConflict("Дождитесь завершения отправки на принтер и повторите.")
        wb_writing = select(Job.id).where(
            Job.kind == WB_ASSEMBLY_KIND, Job.state == "leased", Job.lease_until > instant, assembly_started(),
        ).exists()
        if db.scalar(select(wb_writing)):
            raise ShiftConflict("Дождитесь ответа Wildberries о переводе на сборку и повторите.")
        ozon_writing = select(Job.id).where(
            Job.kind == OZON_ASSEMBLY_KIND, Job.state == "leased", Job.lease_until > instant,
            select(OzonAssembly.job_id).where(
                OzonAssembly.job_id == Job.id, OzonAssembly.started_at.is_not(None),
            ).exists(),
        ).exists()
        if db.scalar(select(ozon_writing)):
            raise ShiftConflict("Дождитесь ответа Ozon о сборке и повторите.")

        db.execute(delete(LabelBatch))
        db.execute(delete(PrintLog))
        db.execute(delete(LabelPrint))
        db.execute(delete(OrderWaiting))
        db.execute(delete(Order))
        db.execute(delete(Job).where(Job.state != "ready"))
        # Revoke cached PDFs now and let the scheduler remove their files.
        db.execute(update(Job).where(Job.state == "ready").values(
            label_until=instant, lease_token=None, lease_until=None, error=None,
        ))
        db.execute(update(Cabinet).values(updated_at=None, error=None))
        for key in db.scalars(select(Cabinet.key)):
            enqueue(db, "sync_orders", key)
        current.revision = uuid4()
        current.started_at = instant
        return current.revision


def assembly_started():
    return select(WbAssembly.job_id).where(WbAssembly.job_id == Job.id, WbAssembly.started_at.is_not(None)).exists()


def claim(db, account_keys, *, wb_assembly=False, wb_auto_assembly=False, ozon_assembly_accounts=()):
    instant = now()
    query = select(Job).where(
        Job.account_key.in_(account_keys),
        or_(
            (Job.state == "pending") & (Job.available_at <= instant),
            (Job.state == "leased") & (Job.lease_until <= instant),
        ),
    ).order_by((Job.kind == "label_pdf").desc(), Job.available_at, Job.id).limit(1)
    if wb_assembly or wb_auto_assembly:
        query = query.where(~assembly_started())
    else:
        query = query.where(Job.kind != WB_ASSEMBLY_KIND)
    if wb_assembly and not wb_auto_assembly:
        selected_supply = select(WbAssembly.job_id).where(
            WbAssembly.job_id == Job.id, WbAssembly.supply_id.is_not(None),
        ).exists()
        query = query.where(or_(Job.kind != WB_ASSEMBLY_KIND, selected_supply))
    if ozon_assembly_accounts:
        query = query.where(or_(Job.kind != OZON_ASSEMBLY_KIND, Job.account_key.in_(ozon_assembly_accounts)))
    else:
        query = query.where(Job.kind != OZON_ASSEMBLY_KIND)
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
    cabinet = db.get(Cabinet, key, with_for_update=True)
    if key == WB_ACCOUNT:
        discard_reassigned_stickers(db, rows)
    db.execute(delete(Order).where(Order.account_key == key))
    db.add_all(Order(
        account_key=key, posting_number=row.posting_number,
        status=row.status, data=row.model_dump(mode="json"),
    ) for row in rows.values())
    cabinet.updated_at = now()
    cabinet.error = catalog_error(result) if key == WB_ACCOUNT else None


def catalog_error(result):
    if result.catalog_access_denied:
        return CATALOG_ERRORS["denied"]
    if result.catalog_status:
        return CATALOG_ERRORS[result.catalog_status]
    if "catalog_status" not in result.model_fields_set and any(
        row.status in WB_BOARD_STATUSES and any(not product.name for product in row.products)
        for row in result.orders
    ):
        return "Обновите и перезапустите агент, чтобы проверить загрузку названий Wildberries."
    return None


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
            state="failed", lease_until=None, error=REASSIGNED_LABEL_ERROR,
        ))
        db.execute(update(Job).where(*filters, Job.state == "ready").values(
            label_until=now(), error=REASSIGNED_LABEL_ERROR,
        ))
        db.execute(delete(LabelPrint).where(LabelPrint.account_key == WB_ACCOUNT, LabelPrint.posting_number.in_(changed)))


def fail_job(db, job, completion):
    marketplace = "Wildberries" if job.account_key == WB_ACCOUNT else "Ozon"
    job.error = ERRORS.get(completion.error, ERRORS["response"]).format(marketplace=marketplace)
    if job.kind == WB_ASSEMBLY_KIND and completion.error == "access":
        job.error = "Wildberries отклонил токен записи. Для WB_WRITE_TOKEN нужны «Маркетплейс» и «Чтение и запись»."
    elif job.kind == OZON_ASSEMBLY_KIND and completion.error == "access":
        job.error = "Ozon отклонил права ключа. Для сборки требуется Posting FBS с доступом на чтение и запись."
    if job.kind == "sync_orders":
        db.get(Cabinet, job.account_key).error = job.error
    started = (job.kind == WB_ASSEMBLY_KIND and db.get(WbAssembly, job.id).started_at is not None
               or job.kind == OZON_ASSEMBLY_KIND and db.get(OzonAssembly, job.id).started_at is not None)
    if not started and completion.status == "retryable_error" and (job.kind == "sync_orders" or job.attempts < 5):
        delay = max(completion.retry_after or 0, min(300, 5 * 2 ** min(job.attempts, 6)))
        job.available_at = now() + timedelta(seconds=delay)
        job.state = "pending"
    else:
        job.state = "failed"
    job.lease_until = None
