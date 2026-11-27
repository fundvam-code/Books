from datetime import timedelta

from sqlalchemy import and_, or_, select

from .db import (
    Heartbeat, Job, LabelBatch, LabelPrint, Monitoring, MonitoringOrder, Order, OrderWaiting,
    OzonAssembly, PrintLog, PrintTask, WbAssembly, write_transaction,
)
from .jobs import (
    ACTIVE, LABEL_STATUSES, OZON_ACCOUNTS, OZON_ASSEMBLY_HEARTBEAT_BASE,
    OZON_ASSEMBLY_KIND, WB_ACCOUNT, WB_AUTO_ASSEMBLY_HEARTBEAT_ID,
    WB_ASSEMBLY_KIND, enqueue, now,
)
from .monitoring_log import append_event
from .printing import TITLE_SPACE_ERROR, batch_item, book_title, pdf_label_number, queue_print

MONITOR_INTERVAL = timedelta(minutes=1)
ASSEMBLY_RETRY_COOLDOWN = timedelta(minutes=5)
MONITOR_ORDER_TERMINAL = ("submitted", "awaiting_confirm", "failed", "unknown")
MONITOR_STOP_ERROR = "Мониторинг остановлен до отправки этикетки на принтер."


def monitoring_enabled(control):
    return bool(control and control.enabled)


def account_enabled(control, account):
    return control.ozon_enabled if account in OZON_ACCOUNTS else control.wb_enabled


def heartbeat_live(db, heartbeat_id, instant):
    heartbeat = db.get(Heartbeat, heartbeat_id)
    return bool(heartbeat and heartbeat.seen_at > instant - timedelta(seconds=120))


def pause_monitoring_work(db, accounts=None):
    instant = now()
    filters = []
    if accounts is not None:
        accounts = tuple(accounts)
        if not accounts:
            return
        filters.append(Job.account_key.in_(accounts))

    assembly_rows = db.execute(
        select(Job, OzonAssembly, WbAssembly)
        .outerjoin(OzonAssembly, OzonAssembly.job_id == Job.id)
        .outerjoin(WbAssembly, WbAssembly.job_id == Job.id)
        .where(
            Job.state.in_(ACTIVE),
            or_(OzonAssembly.automatic.is_(True), WbAssembly.automatic.is_(True)),
            or_(OzonAssembly.started_at.is_(None), WbAssembly.started_at.is_(None)),
            *filters,
        )
        .with_for_update(of=Job)
    )
    for job, ozon_action, wb_action in assembly_rows:
        started = (ozon_action and ozon_action.started_at) or (wb_action and wb_action.started_at)
        if started:
            continue
        job.state = "failed"
        job.error = MONITOR_STOP_ERROR
        job.lease_token = None
        job.lease_until = None

    label_query = (
        select(Job)
        .join(MonitoringOrder, MonitoringOrder.label_job_id == Job.id)
        .where(
            Job.kind == "label_pdf",
            Job.automatic.is_(True),
            Job.state == "pending",
            MonitoringOrder.state == "waiting_label",
            *filters,
        )
        .with_for_update(of=Job)
    )
    for job in db.scalars(label_query):
        job.state = "expired"
        job.error = MONITOR_STOP_ERROR
        job.lease_token = None
        job.lease_until = None

    order_query = select(MonitoringOrder).where(MonitoringOrder.state == "queued")
    if accounts is not None:
        order_query = order_query.where(MonitoringOrder.account_key.in_(accounts))
    monitor_orders = db.scalars(order_query.with_for_update()).all()
    for monitored in monitor_orders:
        task = db.get(PrintTask, monitored.print_task_id, with_for_update=True) if monitored.print_task_id else None
        if task and task.state in ("pending", "leased"):
            task.state = "failed"
            task.error = MONITOR_STOP_ERROR
            task.lease_token = None
            task.lease_until = None
            monitored.state = "waiting_label"
            monitored.print_task_id = None
            monitored.updated_at = instant
            monitored.error = None


def active_print_postings(db, instant):
    rows = db.execute(
        select(PrintTask, LabelBatch)
        .join(LabelBatch, LabelBatch.id == PrintTask.batch_id)
        .where(or_(
            PrintTask.state.in_(("pending", "sending")),
            and_(PrintTask.state == "leased", PrintTask.lease_until > instant),
        ))
    )
    postings = set()
    for _, batch in rows:
        postings.update(
            (item.get("account_key"), item.get("posting_number"))
            for item in batch.labels
            if item.get("account_key") and item.get("posting_number")
        )
    return postings


def active_assembly(db, account_key, posting_number):
    return db.scalar(select(Job.id).where(
        Job.account_key == account_key,
        Job.posting_number == posting_number,
        Job.kind.in_((OZON_ASSEMBLY_KIND, WB_ASSEMBLY_KIND)),
        Job.state.in_(ACTIVE),
    ).limit(1)) is not None


def recent_automatic_assembly_failure(db, account_key, posting_number, instant):
    return db.scalar(
        select(Job.id)
        .outerjoin(OzonAssembly, OzonAssembly.job_id == Job.id)
        .outerjoin(WbAssembly, WbAssembly.job_id == Job.id)
        .where(
            Job.account_key == account_key,
            Job.posting_number == posting_number,
            Job.kind.in_((OZON_ASSEMBLY_KIND, WB_ASSEMBLY_KIND)),
            Job.state == "failed",
            Job.available_at > instant - ASSEMBLY_RETRY_COOLDOWN,
            or_(OzonAssembly.automatic.is_(True), WbAssembly.automatic.is_(True)),
        )
        .limit(1)
    ) is not None


def ensure_label_job(db, order, label_dir):
    ready = db.scalar(select(Job.id).where(
        Job.kind == "label_pdf", Job.account_key == order.account_key,
        Job.posting_number == order.posting_number, Job.state == "ready",
        Job.label_until > now(),
    ).order_by(Job.label_until.desc()).limit(1))
    if ready is not None and (label_dir / f"{ready}.pdf").is_file():
        return ready
    active = db.scalar(select(Job.id).where(
        Job.kind == "label_pdf", Job.account_key == order.account_key,
        Job.posting_number == order.posting_number, Job.state.in_(ACTIVE),
    ).limit(1))
    return active or enqueue(db, "label_pdf", order.account_key, order.posting_number, automatic=True)


def queue_monitor_print(db, order, monitored, label_job_id, label_dir):
    item = batch_item(order, label_job_id, True)
    task_id = queue_print(
        db, order.account_key, [item], automatic=True,
    )
    monitored.state = "queued"
    monitored.label_job_id = label_job_id
    monitored.print_task_id = task_id
    monitored.updated_at = now()
    monitored.error = None
    append_event(
        db, order, "print_queued", "Задание поставлено в очередь печати",
        event_key=f"print-queued:{task_id}",
        label_number=pdf_label_number(label_dir / f"{label_job_id}.pdf", order.account_key),
    )


def monitor_order(db, order, label_dir, active_prints, instant):
    key = (order.account_key, order.posting_number)
    monitored = db.get(MonitoringOrder, key, with_for_update=True)
    if monitored and monitored.state in MONITOR_ORDER_TERMINAL:
        if monitored.state in ("failed", "unknown") and monitored.error:
            prior_task = db.get(PrintTask, monitored.print_task_id) if monitored.print_task_id else None
            prior_batch = db.get(LabelBatch, prior_task.batch_id) if prior_task else None
            legacy_strict_title = bool(
                prior_task and prior_batch and prior_task.error == TITLE_SPACE_ERROR
                and any(
                    item.get("account_key") == order.account_key
                    and item.get("posting_number") == order.posting_number
                    and item.get("require_complete")
                    for item in prior_batch.labels
                )
            )
            if monitored.print_task_id:
                item = next((item for item in prior_batch.labels
                             if item.get("account_key") == order.account_key
                             and item.get("posting_number") == order.posting_number), {}) if prior_batch else {}
                label_job_id = item.get("job_id")
                label_number = (
                    pdf_label_number(label_dir / f"{label_job_id}.pdf", order.account_key)
                    if label_job_id else None
                )
                append_event(
                    db, order, monitored.state,
                    "Неизвестный результат отправки на печать"
                    if monitored.state == "unknown" else "Ошибка печати",
                    event_key=f"print-{monitored.state}:{monitored.print_task_id}:{order.account_key}:{order.posting_number}",
                    error=monitored.error,
                    label_number=label_number,
                    event_at=monitored.updated_at,
                )
                if legacy_strict_title:
                    monitored.state = "waiting_label"
                    monitored.print_task_id = None
                    monitored.error = None
                else:
                    return
            elif monitored.label_job_id:
                append_event(
                    db, order, monitored.state, "Не удалось получить этикетку",
                    event_key=f"label-failed:{monitored.label_job_id}",
                    error=monitored.error,
                    event_at=monitored.updated_at,
                )
                return
            else:
                return
    if active_assembly(db, *key) or key in active_prints:
        return

    title = book_title(order)
    products = order.data.get("products", [])
    if not title or not products or any(not " ".join((item.get("name") or "").split()) for item in products):
        return

    label_job_id = None
    if monitored and monitored.label_job_id:
        label_job = db.get(Job, monitored.label_job_id)
        if label_job is None or label_job.state == "expired":
            monitored.label_job_id = None
        elif label_job.state == "failed":
            monitored.state = "failed"
            monitored.updated_at = instant
            monitored.error = label_job.error
            append_event(
                db, order, "failed", "Не удалось получить этикетку",
                event_key=f"label-failed:{label_job.id}", error=label_job.error,
            )
            return
        elif label_job.state in ACTIVE:
            return
        elif label_job.state == "ready" and label_job.label_until > instant:
            if (label_dir / f"{label_job.id}.pdf").is_file():
                label_job_id = label_job.id
            else:
                monitored.label_job_id = None
        else:
            monitored.label_job_id = None

    if label_job_id is None:
        label_job_id = ensure_label_job(db, order, label_dir)
        if label_job_id is None:
            return
        label_job = db.get(Job, label_job_id)
        if monitored is None:
            monitored = MonitoringOrder(
                account_key=order.account_key,
                posting_number=order.posting_number,
                state="waiting_label",
                label_job_id=label_job_id,
                updated_at=instant,
            )
            db.add(monitored)
        else:
            monitored.state = "waiting_label"
            monitored.label_job_id = label_job_id
            monitored.updated_at = instant
        append_event(
            db, order, "label_requested", "Запрошена этикетка",
            event_key=f"label-requested:{order.account_key}:{order.posting_number}:{label_job_id}",
        )
        if label_job.state != "ready" or label_job.label_until <= instant:
            return
        if not (label_dir / f"{label_job_id}.pdf").is_file():
            return
    elif monitored is None:
        monitored = MonitoringOrder(
            account_key=order.account_key,
            posting_number=order.posting_number,
            state="waiting_label",
            label_job_id=label_job_id,
            updated_at=instant,
        )
        db.add(monitored)
        append_event(
            db, order, "label_requested", "Этикетка готова к печати",
            event_key=f"label-requested:{order.account_key}:{order.posting_number}:{label_job_id}",
        )

    queue_monitor_print(db, order, monitored, label_job_id, label_dir)


def run_monitor_cycle(label_dir):
    instant = now()
    with write_transaction() as db:
        control = db.get(Monitoring, 1, with_for_update=True)
        if not monitoring_enabled(control):
            pause_monitoring_work(db)
            return
        if control.checked_at and control.checked_at > instant - MONITOR_INTERVAL:
            return
        control.checked_at = instant

        accounts = []
        if control.ozon_enabled:
            accounts.extend(OZON_ACCOUNTS)
        if control.wb_enabled:
            accounts.append(WB_ACCOUNT)
        if not accounts:
            return

        waiting = set(db.execute(select(OrderWaiting.account_key, OrderWaiting.posting_number)))
        printed = set(db.execute(select(LabelPrint.account_key, LabelPrint.posting_number)))
        pending_confirmation = set(db.execute(select(PrintLog.account_key, PrintLog.posting_number).where(
            PrintLog.awaiting_confirmation.is_(True),
        )))
        orders = db.scalars(select(Order).where(
            Order.account_key.in_(accounts),
            Order.status.in_(("awaiting_packaging", "awaiting_deliver", "wb_new", "wb_confirm")),
        ).order_by(Order.account_key, Order.posting_number).with_for_update()).all()
        active_prints = active_print_postings(db, instant)

        for order in orders:
            key = (order.account_key, order.posting_number)
            if key in waiting or key in printed or key in pending_confirmation:
                continue
            append_event(
                db, order, "found", "Заказ найден мониторингом",
                event_key=f"found:{order.account_key}:{order.posting_number}",
            )

            if order.status in ("awaiting_packaging", "wb_new"):
                if active_assembly(db, *key) or recent_automatic_assembly_failure(db, *key, instant):
                    continue
                if order.account_key in OZON_ACCOUNTS:
                    if not heartbeat_live(db, OZON_ASSEMBLY_HEARTBEAT_BASE + int(order.account_key), instant):
                        continue
                    job_id = enqueue(db, OZON_ASSEMBLY_KIND, order.account_key, order.posting_number)
                    if job_id:
                        db.add(OzonAssembly(job_id=job_id, print_after=False, automatic=True))
                        append_event(
                            db, order, "assembly_queued", "Заказ отправлен в сборку",
                            event_key=f"assembly-queued:{job_id}",
                        )
                else:
                    if not heartbeat_live(db, WB_AUTO_ASSEMBLY_HEARTBEAT_ID, instant):
                        continue
                    job_id = enqueue(db, WB_ASSEMBLY_KIND, WB_ACCOUNT, order.posting_number)
                    if job_id:
                        db.add(WbAssembly(job_id=job_id, print_after=False, automatic=True))
                        append_event(
                            db, order, "assembly_queued", "Заказ отправлен в сборку",
                            event_key=f"assembly-queued:{job_id}",
                        )
            elif order.status in LABEL_STATUSES:
                monitor_order(db, order, label_dir, active_prints, instant)
