import asyncio
import csv
import logging
import os
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from secrets import compare_digest
from typing import Annotated, Literal
from urllib.parse import quote, urlencode
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, Form, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image
from pypdf import PdfWriter
from pypdf.errors import PyPdfError
from pypdfium2 import PdfiumError
from sqlalchemy import and_, delete, desc, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from .book_storage import StorageLookup, refresh_catalog
from .db import (BookStorage, Cabinet, Heartbeat, Job, LabelBatch, LabelPrint, Order, OrderWaiting, OzonAssembly, PrintLog, PrintTask, Session, Shift, WbAssembly,
                 write_transaction)
from .jobs import (ACTIVE, LABEL_STATUSES, OZON_ACCOUNTS, OZON_STATUSES, PDF_LIMIT, WB_ACCOUNT, WB_ASSEMBLY_KIND,
                   OZON_ASSEMBLY_KIND, OZON_ASSEMBLY_HEARTBEAT_BASE, OZON_PRINT_HEARTBEAT_BASE,
                   WB_AUTO_ASSEMBLY_HEARTBEAT_ID, WB_BOARD_STATUSES,
                   ShiftConflict, apply_snapshot, claim, enqueue, fail_job, initialize, maintain, now, start_shift)
from .label_title import NoTitleSpace, add_title
from .printing import (PRINT_LEASE, TITLE_SPACE_ERROR, BatchTitleSpace, backfill_print_log,
                       batch_item, batch_versions, batch_writer, book_title,
                       claim_print, create_batch, label_metadata, mark_submitted_labels, pdf_label_number,
                       queue_print, record_print)
from .schemas import Account, AccountFilter, AgentAccounts, AgentHeartbeat, AssemblyStart, Completion, LabelTask, PrintResult
from .wb_label import sticker_pdf

ROOT = Path(__file__).parent
LABEL_DIR = Path(os.environ.get("LABEL_DIR", "/labels"))
MSK = timezone(timedelta(hours=3), "MSK")
templates = Jinja2Templates(directory=ROOT / "templates")
cabinet_names = {"1": "Золотая антилопа", "2": "Зеленая книга", WB_ACCOUNT: "Wildberries"}
templates.env.globals.update(cabinet_names=cabinet_names, label_statuses=LABEL_STATUSES, wb_account=WB_ACCOUNT)
labels = dict(zip((*OZON_STATUSES, *WB_BOARD_STATUSES),
                  ("Ожидают сборки", "Собраны — ждут отгрузки", "Новые", "На сборке")))


def format_msk(value: datetime | str | None) -> str:
    if not value:
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(MSK).strftime("%d.%m.%Y %H:%M %Z")


def format_date_msk(value: datetime | str | None) -> str:
    if not value:
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(MSK).strftime("%d.%m.%Y")


templates.env.filters["msk"] = format_msk
templates.env.filters["date_msk"] = format_date_msk


def orders_url(account: str, hide_printed: bool = False, hide_waiting: bool = True) -> str:
    options = {"account": account}
    if hide_printed:
        options["hide_printed"] = "true"
    if not hide_waiting:
        options["hide_waiting"] = "false"
    return "/orders?" + urlencode(options)


templates.env.globals["orders_url"] = orders_url


async def scheduler():
    while True:
        try:
            await asyncio.to_thread(maintain, LABEL_DIR)
        except Exception:
            logging.getLogger(__name__).error("Не удалось обслужить очередь или удалить этикетки")
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app):
    if len(os.environ.get("AGENT_SHARED_SECRET", "")) < 32 or not os.environ.get("PUBLIC_DOMAIN"):
        raise RuntimeError("Укажите PUBLIC_DOMAIN и секрет агента длиной от 32 символов")
    LABEL_DIR.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(initialize)
    await asyncio.to_thread(backfill_print_log, LABEL_DIR)
    task = asyncio.create_task(scheduler())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    return JSONResponse({"detail": "Некорректный запрос"}, status_code=422)


def agent_auth(authorization: Annotated[str, Header()] = ""):
    expected = "Bearer " + os.environ["AGENT_SHARED_SECRET"]
    if not compare_digest(authorization.encode(), expected.encode()):
        raise HTTPException(401, "Доступ агента отклонён")


Agent = Annotated[None, Depends(agent_auth)]
Lease = Annotated[UUID, Header(alias="X-Lease-Token")]


def locked_job(db, job_id, token, *, allow_ready=False):
    job = db.get(Job, job_id, with_for_update=True)
    if job is None or job.lease_token != token:
        raise HTTPException(409, "Задание больше не закреплено за этой попыткой")
    if allow_ready and job.state == "ready":
        return job
    if job.state != "leased" or job.lease_until <= now():
        raise HTTPException(409, "Аренда задания истекла")
    return job


@app.get("/health")
def health():
    with Session() as db:
        db.get(Cabinet, "1")
    return {"status": "ok"}


@app.post("/api/agent/heartbeat", status_code=204)
def heartbeat(body: AgentHeartbeat, auth: Agent):
    with write_transaction() as db:
        stamp = now()
        db.execute(insert(Heartbeat).values(id=1, seen_at=stamp).on_conflict_do_update(
            index_elements=["id"], set_={"seen_at": stamp},
        ))
        if WB_ACCOUNT in body.account_keys:
            created = db.scalar(insert(Cabinet).values(key=WB_ACCOUNT).on_conflict_do_nothing().returning(Cabinet.key))
            if created:
                enqueue(db, "sync_orders", WB_ACCOUNT)
            if body.wb_auto_assembly:
                db.execute(insert(Heartbeat).values(id=WB_AUTO_ASSEMBLY_HEARTBEAT_ID, seen_at=stamp).on_conflict_do_update(
                    index_elements=["id"], set_={"seen_at": stamp},
                ))
        for key in body.ozon_assembly_accounts:
            heartbeat_id = OZON_ASSEMBLY_HEARTBEAT_BASE + int(key)
            db.execute(insert(Heartbeat).values(id=heartbeat_id, seen_at=stamp).on_conflict_do_update(
                index_elements=["id"], set_={"seen_at": stamp},
            ))
        for key in body.ozon_print_accounts:
            heartbeat_id = OZON_PRINT_HEARTBEAT_BASE + int(key)
            db.execute(insert(Heartbeat).values(id=heartbeat_id, seen_at=stamp).on_conflict_do_update(
                index_elements=["id"], set_={"seen_at": stamp},
            ))
    return Response(status_code=204)


@app.post("/api/agent/jobs/claim")
def claim_job(auth: Agent, body: AgentAccounts | None = None):
    with write_transaction() as db:
        job = claim(db, body.account_keys if body else OZON_ACCOUNTS,
                    wb_assembly=bool(body and body.wb_assembly), wb_auto_assembly=bool(body and body.wb_auto_assembly),
                    ozon_assembly_accounts=body.ozon_assembly_accounts if body else ())
        if job is None:
            return Response(status_code=204)
        payload = {}
        if job.kind == "label_pdf":
            payload = {"posting_number": job.posting_number, "ozon_task_id": job.ozon_task_id}
        elif job.kind == WB_ASSEMBLY_KIND:
            payload = {"posting_number": job.posting_number, "supply_id": db.get(WbAssembly, job.id).supply_id}
        elif job.kind == OZON_ASSEMBLY_KIND:
            action = db.get(OzonAssembly, job.id)
            order = db.get(Order, (job.account_key, job.posting_number))
            payload = {
                "posting_number": job.posting_number,
                "print_after": action.print_after,
                "products": [{"sku": product.get("sku"), "quantity": product.get("quantity")}
                             for product in (order.data.get("products", []) if order else [])],
            }
        else:
            payload = {"known_postings": [
                {"posting_number": number, "created_at": data.get("created_at")}
                for number, data in db.execute(select(Order.posting_number, Order.data).where(
                    Order.account_key == job.account_key,
                ))
            ]}
        return dict(id=job.id, account_key=job.account_key, kind=job.kind,
                    lease_token=job.lease_token, payload=payload)


@app.post("/api/agent/jobs/{job_id}/renew", status_code=204)
def renew_job(job_id: UUID, auth: Agent, token: Lease):
    with write_transaction() as db:
        job = locked_job(db, job_id, token)
        job.lease_until = now() + timedelta(seconds=90)
    return Response(status_code=204)


@app.post("/api/agent/jobs/{job_id}/assembly-start", status_code=204)
def start_wb_assembly(job_id: UUID, auth: Agent, token: Lease, body: AssemblyStart | None = None):
    with write_transaction() as db:
        job = locked_job(db, job_id, token)
        if job.kind == OZON_ASSEMBLY_KIND:
            action = db.get(OzonAssembly, job.id)
            order = db.get(Order, (job.account_key, job.posting_number), with_for_update=True)
            if action is None or action.started_at is not None or order is None or order.status != "awaiting_packaging":
                raise HTTPException(409, "Заказ Ozon уже изменён или сборка уже начата")
            action.started_at = now()
            job.lease_until = action.started_at + timedelta(seconds=90)
            return Response(status_code=204)
        action = db.get(WbAssembly, job.id) if job.kind == WB_ASSEMBLY_KIND else None
        if action is None or action.started_at is not None:
            raise HTTPException(409, "Перевод уже начат или недоступен")
        if body is not None:
            if action.supply_id and action.supply_id != body.supply_id:
                raise HTTPException(409, "Поставка не совпадает с заданием")
            action.supply_id = body.supply_id
        if not action.supply_id:
            raise HTTPException(409, "Открытая поставка ещё не определена")
        action.started_at = now()
        job.lease_until = action.started_at + timedelta(seconds=90)
    return Response(status_code=204)


@app.post("/api/agent/jobs/{job_id}/complete", status_code=204)
def complete_job(job_id: UUID, body: Completion, auth: Agent, token: Lease):
    with write_transaction() as db:
        job = locked_job(db, job_id, token)
        if body.status == "succeeded":
            if job.kind == WB_ASSEMBLY_KIND and db.get(WbAssembly, job.id).started_at is not None:
                action = db.get(WbAssembly, job.id)
                if action.print_after:
                    order = db.get(Order, (WB_ACCOUNT, job.posting_number), with_for_update=True)
                    if order and (
                        order.status == "wb_new" or
                        (order.status == "wb_confirm" and order.data.get("supply_id") == action.supply_id)
                    ):
                        order.status = "wb_confirm"
                        order.data = {**order.data, "status": "wb_confirm", "supply_id": action.supply_id}
                        label_job_id, _ = get_or_enqueue_label(db, order)
                        if label_job_id is not None:
                            action.print_task_id = queue_print(db, WB_ACCOUNT, [batch_item(order, label_job_id, False)])
                enqueue(db, "sync_orders", WB_ACCOUNT)
                db.execute(update(Job).where(
                    Job.kind == "sync_orders", Job.account_key == WB_ACCOUNT, Job.state == "pending",
                    Job.available_at > now(),
                ).values(available_at=now()))
            elif job.kind == OZON_ASSEMBLY_KIND:
                action = db.get(OzonAssembly, job.id)
                order = db.get(Order, (job.account_key, job.posting_number), with_for_update=True)
                if (action is None or action.started_at is None or order is None
                        or body.assembly_status != "awaiting_deliver"
                        or order.status not in ("awaiting_packaging", "awaiting_deliver")):
                    raise HTTPException(422, "Ozon не подтвердил сборку отправления")
                order.status = "awaiting_deliver"
                order.data = {**order.data, "status": "awaiting_deliver"}
                if action.print_after:
                    label_job_id = enqueue(db, "label_pdf", job.account_key, job.posting_number)
                    if label_job_id is None:
                        label_job_id = db.scalar(select(Job.id).where(
                            Job.kind == "label_pdf", Job.account_key == job.account_key,
                            Job.posting_number == job.posting_number, Job.state.in_(ACTIVE),
                        ))
                    if label_job_id is None:
                        raise HTTPException(409, "Не удалось создать задание этикетки")
                    action.label_job_id = label_job_id
                enqueue(db, "sync_orders", job.account_key)
                db.execute(update(Job).where(
                    Job.kind == "sync_orders", Job.account_key == job.account_key,
                    Job.state == "pending", Job.available_at > now(),
                ).values(available_at=now()))
            elif job.kind == "sync_orders" and body.result is not None:
                try:
                    apply_snapshot(db, job.account_key, body.result)
                except ValueError as error:
                    raise HTTPException(422, str(error)) from error
            else:
                raise HTTPException(422, "Ожидался снимок заказов")
            job.state = "done"
            job.lease_until = None
            job.error = None
        else:
            fail_job(db, job, body)
    return Response(status_code=204)


@app.put("/api/agent/jobs/{job_id}/label-task", status_code=204)
def save_label_task(job_id: UUID, body: LabelTask, auth: Agent, token: Lease):
    with write_transaction() as db:
        job = locked_job(db, job_id, token)
        if (job.kind != "label_pdf" or job.account_key == WB_ACCOUNT
                or job.ozon_task_id not in (None, body.ozon_task_id)):
            raise HTTPException(409, "Идентификатор задания уже закреплён")
        job.ozon_task_id = body.ozon_task_id
    return Response(status_code=204)


def publish_pdf(job_id, token, data, *, wb=False):
    with write_transaction() as db:
        job = locked_job(db, job_id, token, allow_ready=True)
        if job.kind != "label_pdf" or wb != (job.account_key == WB_ACCOUNT):
            raise HTTPException(409, "Тип этикетки не соответствует заданию")
        if job.state == "ready":
            return
        if not wb and job.ozon_task_id is None:
            raise HTTPException(409, "Задание генерации не зарегистрировано")
        temporary = LABEL_DIR / f".{job.id}.{token}.tmp"
        try:
            temporary.write_bytes(data)
            temporary.replace(LABEL_DIR / f"{job.id}.pdf")
        finally:
            temporary.unlink(missing_ok=True)
        job.state = "ready"
        job.label_until = now() + timedelta(hours=24)
        job.lease_until = None
        action = db.scalar(select(OzonAssembly).where(OzonAssembly.label_job_id == job.id).with_for_update())
        if action is not None and action.print_after and action.print_task_id is None:
            order = db.get(Order, (job.account_key, job.posting_number))
            if order is not None:
                action.print_task_id = queue_print(db, job.account_key, [batch_item(order, job.id, False)])


async def read_label_upload(request, content_type):
    if request.headers.get("content-type") != content_type:
        raise HTTPException(415, "Неподдерживаемый формат этикетки")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > PDF_LIMIT:
            raise HTTPException(413, "Файл этикетки превышает 8 МБ")
        data.extend(chunk)
    return data


@app.put("/api/agent/jobs/{job_id}/label", status_code=204)
async def upload_pdf(job_id: UUID, request: Request, auth: Agent, token: Lease):
    data = await read_label_upload(request, "application/pdf")
    if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-2048:]:
        raise HTTPException(422, "Некорректный PDF")
    await asyncio.to_thread(publish_pdf, job_id, token, data)
    return Response(status_code=204)


@app.put("/api/agent/jobs/{job_id}/wb-sticker", status_code=204)
async def upload_wb_sticker(job_id: UUID, request: Request, auth: Agent, token: Lease,
                            number: Annotated[str, Header(alias="X-Label-Number",
                                                          pattern=r"^[A-Za-z0-9-]{1,40} [A-Za-z0-9-]{1,40}$")]):
    png = await read_label_upload(request, "image/png")
    try:
        data = await asyncio.to_thread(sticker_pdf, png, number)
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise HTTPException(422, "Некорректный PNG стикера Wildberries") from error
    await asyncio.to_thread(publish_pdf, job_id, token, data, wb=True)
    return Response(status_code=204)


@app.get("/")
def home():
    return RedirectResponse("/orders", status_code=303)


def ready_label_id(account_key, posting_number):
    return select(Job.id).where(
        Job.kind == "label_pdf", Job.account_key == account_key, Job.posting_number == posting_number,
        Job.state == "ready", Job.label_until > now(),
    ).order_by(desc(Job.label_until)).limit(1)


def orders_with_labels():
    return select(
        Order,
        LabelPrint.posting_number.is_not(None),
        ready_label_id(Order.account_key, Order.posting_number).scalar_subquery(),
        OrderWaiting.posting_number.is_not(None),
    ).outerjoin(LabelPrint, and_(
        LabelPrint.account_key == Order.account_key,
        LabelPrint.posting_number == Order.posting_number,
    )).outerjoin(OrderWaiting, and_(
        OrderWaiting.account_key == Order.account_key,
        OrderWaiting.posting_number == Order.posting_number,
    ))


def has_printed_label():
    return select(LabelPrint.posting_number).where(
        LabelPrint.account_key == Order.account_key,
        LabelPrint.posting_number == Order.posting_number,
    ).exists()


def order_stage_counts(db, accounts):
    waiting = select(OrderWaiting.posting_number).where(
        OrderWaiting.account_key == Order.account_key,
        OrderWaiting.posting_number == Order.posting_number,
    ).exists()
    rows = db.execute(
        select(
            Order.account_key,
            func.count().filter(Order.status.in_(("awaiting_packaging", "wb_new"))),
            func.count().filter(Order.status.in_(LABEL_STATUSES), ~has_printed_label()),
        ).where(Order.account_key.in_(accounts), ~waiting).group_by(Order.account_key)
    )
    counts = {key: [0, 0] for key in accounts}
    for key, assembly, shipment in rows:
        counts[key] = [assembly, shipment]
    return {**counts, "all": [sum(pair[0] for pair in counts.values()), sum(pair[1] for pair in counts.values())]}


def pdf_is_available(job_id):
    return job_id is not None and (LABEL_DIR / f"{job_id}.pdf").is_file()


def label_number(job_id: UUID | None, account_key: str) -> str | None:
    return pdf_label_number(LABEL_DIR / f"{job_id}.pdf", account_key) if job_id is not None else None


templates.env.globals["label_number"] = label_number
templates.env.globals["book_title"] = book_title


def get_or_enqueue_label(db, order):
    job_id = db.scalar(ready_label_id(order.account_key, order.posting_number))
    if pdf_is_available(job_id):
        return job_id, True
    job_id = db.scalar(select(Job.id).where(
        Job.kind == "label_pdf", Job.account_key == order.account_key,
        Job.posting_number == order.posting_number, Job.state.in_(ACTIVE),
    ))
    return job_id or enqueue(db, "label_pdf", order.account_key, order.posting_number), False


@app.get("/orders")
def orders(request: Request, account: str = "all", hide_printed: bool = False, shift_started: UUID | None = None,
           hide_waiting: bool = True, storage_refresh: Literal["updated", "failed", "unconfigured"] | None = None):
    with Session() as db:
        registered = {cabinet.key: cabinet for cabinet in db.scalars(select(Cabinet).order_by(Cabinet.key))}
        if account != "all" and account not in registered:
            raise HTTPException(404, "Кабинет не найден")
        keys = tuple(registered) if account == "all" else (account,)
        cabinets = [registered[key] for key in keys]
        seen = db.get(Heartbeat, 1)
        catalog = db.get(BookStorage, 1)
        shift = db.get(Shift, 1) if shift_started else None
        recent_shift = bool(shift and shift.revision == shift_started and shift.started_at
                            and now() < shift.started_at + timedelta(seconds=90))
        stage_counts = order_stage_counts(db, tuple(registered))
        query = orders_with_labels().where(Order.account_key.in_(keys), Order.status.in_(tuple(labels)))
        if hide_printed:
            query = query.where(or_(LabelPrint.posting_number.is_(None), OrderWaiting.posting_number.is_not(None)))
        records = db.execute(query).all()
        ozon_assembly_accounts = {key for key in keys if key in OZON_ACCOUNTS and db.get(
            Heartbeat, OZON_ASSEMBLY_HEARTBEAT_BASE + int(key),
        ) and db.get(Heartbeat, OZON_ASSEMBLY_HEARTBEAT_BASE + int(key)).seen_at >= now() - timedelta(seconds=120)}
        ozon_print_accounts = {key for key in keys if key in OZON_ACCOUNTS and db.get(
            Heartbeat, OZON_PRINT_HEARTBEAT_BASE + int(key),
        ) and db.get(Heartbeat, OZON_PRINT_HEARTBEAT_BASE + int(key)).seen_at >= now() - timedelta(seconds=120)}
    records.sort(key=lambda row: (row[0].data.get("shipment_date") or "~", row[0].posting_number))
    orders_by_status = {status: [] for status in labels}
    waiting_orders = []
    has_printable = False
    for order, printed, job_id, waiting in records:
        printable = order.status in LABEL_STATUSES
        card = (order, printed, job_id if pdf_is_available(job_id) else None, waiting)
        if waiting:
            waiting_orders.append(card)
        if (not waiting or not hide_waiting) and (not printed or not hide_printed):
            orders_by_status[order.status].append(card)
            has_printable |= printable and not waiting
    boards = []
    if any(key in OZON_ACCOUNTS for key in keys):
        boards.append(("Ozon", [(labels[status], orders_by_status[status]) for status in OZON_STATUSES]))
    if WB_ACCOUNT in keys:
        boards.append(("Wildberries", [(labels[status], orders_by_status[status]) for status in WB_BOARD_STATUSES]))
    return templates.TemplateResponse(request, "orders.html", {
        "account": account, "cabinets": cabinets, "boards": boards,
        "active_tab_title": cabinet_names[account] if account != "all" else "Все кабинеты",
        "active_tab_count": stage_counts[account],
        "tabs": [
            ("all", "Все кабинеты", stage_counts["all"]),
            *((key, cabinet_names[key], stage_counts[key]) for key in registered),
        ],
        "hide_printed": hide_printed,
        "hide_waiting": hide_waiting,
        "storage_match": StorageLookup(catalog.entries if catalog else []).match,
        "storage_updated_at": catalog.updated_at if catalog else None,
        "storage_refresh": storage_refresh,
        "waiting_orders": waiting_orders,
        "order_status_labels": labels,
        "recent_shift": recent_shift,
        "shift_loading": recent_shift and any(cabinet.updated_at is None and not cabinet.error for cabinet in cabinets),
        "has_printable": has_printable,
        "online": bool(seen and seen.seen_at >= now() - timedelta(seconds=120)),
        "ozon_assembly_accounts": ozon_assembly_accounts,
        "ozon_print_accounts": ozon_print_accounts,
    })


@app.get("/orders/stage-counts")
def stage_counts():
    with Session() as db:
        accounts = tuple(db.scalars(select(Cabinet.key)))
        return order_stage_counts(db, accounts)


@app.get("/orders/{account_key}/{posting_number}")
def detail(request: Request, account_key: Account, posting_number: str):
    with Session() as db:
        row = db.execute(orders_with_labels().where(
            Order.account_key == account_key, Order.posting_number == posting_number,
            Order.status.in_(tuple(labels)),
        )).one_or_none()
        catalog = db.get(BookStorage, 1)
    if row is None:
        raise HTTPException(404, "Активное отправление не найдено")
    order, printed, job_id, waiting = row
    return templates.TemplateResponse(request, "detail.html", {
        "order": order, "data": order.data, "status_label": labels[order.status],
        "printed": printed, "label_job_id": job_id if pdf_is_available(job_id) else None,
        "waiting": waiting,
        "storage_match": StorageLookup(catalog.entries if catalog else []).match,
    })


def require_dashboard_origin(request: Request):
    if request.headers.get("origin") != "https://" + os.environ["PUBLIC_DOMAIN"]:
        raise HTTPException(403, "Откройте форму печати в дашборде")


@app.post("/book-storage/refresh")
def refresh_book_storage(request: Request, account: Annotated[AccountFilter, Form()] = "all",
                         hide_printed: Annotated[bool, Form()] = False,
                         hide_waiting: Annotated[bool, Form()] = True):
    if request.headers.get("origin") != "https://" + os.environ["PUBLIC_DOMAIN"]:
        raise HTTPException(403, "Откройте дашборд для обновления мест хранения")
    try:
        result = refresh_catalog()
    except (httpx.HTTPError, csv.Error, ValueError) as error:
        logging.getLogger(__name__).warning("Не удалось обновить места хранения (%s)", type(error).__name__)
        result = "failed"
    return RedirectResponse(f"{orders_url(account, hide_printed, hide_waiting)}&storage_refresh={result}#book-storage",
                            status_code=303)


def wb_write_online(db):
    agent = db.get(Heartbeat, WB_AUTO_ASSEMBLY_HEARTBEAT_ID)
    return bool(agent and agent.seen_at >= now() - timedelta(seconds=120))


def ozon_capability_online(db, account_key, *, printing=False):
    heartbeat_id = (OZON_PRINT_HEARTBEAT_BASE if printing else OZON_ASSEMBLY_HEARTBEAT_BASE) + int(account_key)
    agent = db.get(Heartbeat, heartbeat_id)
    return bool(agent and agent.seen_at >= now() - timedelta(seconds=120))


@app.post("/orders/{account_key}/{posting_number}/assembly")
def request_ozon_assembly(request: Request, account_key: Account, posting_number: str,
                          print_after: Annotated[bool, Form()] = False):
    require_dashboard_origin(request)
    if account_key not in OZON_ACCOUNTS:
        raise HTTPException(404, "Отправление Ozon не найдено")
    with write_transaction() as db:
        order = db.get(Order, (account_key, posting_number), with_for_update=True)
        if order is None or order.status != "awaiting_packaging":
            raise HTTPException(409, "Заказ больше не ожидает сборки")
        if db.get(OrderWaiting, (account_key, posting_number)):
            raise HTTPException(409, "Сначала верните отправление из ожидания")
        if not ozon_capability_online(db, account_key):
            raise HTTPException(409, "Локальный агент Ozon с правом сборки сейчас не в сети")
        if print_after and not ozon_capability_online(db, account_key, printing=True):
            raise HTTPException(409, "Для прямой печати запустите агент на Mac с настроенным принтером")
        job_id = enqueue(db, OZON_ASSEMBLY_KIND, account_key, posting_number)
        if job_id is not None:
            db.add(OzonAssembly(job_id=job_id, print_after=print_after))
        else:
            job_id = db.scalar(select(Job.id).where(
                Job.kind == OZON_ASSEMBLY_KIND, Job.account_key == account_key,
                Job.posting_number == posting_number, Job.state.in_(ACTIVE),
            ))
            if job_id is None:
                raise HTTPException(409, "Сборка этого заказа уже обрабатывается")
            action = db.get(OzonAssembly, job_id)
            if action is None or action.print_after != print_after:
                raise HTTPException(409, "Для заказа уже выполняется другое действие")
    return RedirectResponse(f"/ozon-assembly/{job_id}", status_code=303)


@app.get("/ozon-assembly/{job_id}")
def ozon_assembly_status(request: Request, job_id: UUID):
    with Session() as db:
        job = db.get(Job, job_id)
        action = db.get(OzonAssembly, job_id) if job and job.kind == OZON_ASSEMBLY_KIND else None
        if action is None:
            raise HTTPException(404, "Задание сборки Ozon не найдено")
        label_job = db.get(Job, action.label_job_id) if action.label_job_id else None
        print_task = db.get(PrintTask, action.print_task_id) if action.print_task_id else None
        order = db.get(Order, (job.account_key, job.posting_number))
        title = book_title(order)
        if not title and print_task:
            batch = db.get(LabelBatch, print_task.batch_id)
            if batch:
                title = next((item["book_title"] for item in batch.labels if item.get("book_title")), "")
    return templates.TemplateResponse(request, "ozon_assembly_status.html", {
        "job": job, "action": action, "label_job": label_job, "print_task": print_task,
        "title": title,
    })


@app.get("/orders/w/{posting_number}/assembly")
def wb_assembly_form(request: Request, posting_number: str):
    with Session() as db:
        order = db.get(Order, (WB_ACCOUNT, posting_number))
        if order is None or order.status != "wb_new":
            raise HTTPException(404, "Новое задание Wildberries не найдено")
        enabled = wb_write_online(db)
    return templates.TemplateResponse(request, "wb_assembly.html", {
        "order": order, "enabled": enabled,
    })


@app.post("/orders/w/{posting_number}/assembly")
def request_wb_assembly(request: Request, posting_number: str,
                        print_after: Annotated[bool, Form()] = False):
    if request.headers.get("origin") != "https://" + os.environ["PUBLIC_DOMAIN"]:
        raise HTTPException(403, "Откройте форму перевода на сборку в дашборде")
    with write_transaction() as db:
        order = db.get(Order, (WB_ACCOUNT, posting_number), with_for_update=True)
        if order is None or order.status != "wb_new":
            raise HTTPException(409, "Задание больше не находится в «Новых»")
        if db.get(OrderWaiting, (WB_ACCOUNT, posting_number)):
            raise HTTPException(409, "Сначала верните отправление из ожидания")
        if not wb_write_online(db):
            raise HTTPException(409, "Агент с настроенным WB_WRITE_TOKEN сейчас недоступен")
        job_id = enqueue(db, WB_ASSEMBLY_KIND, WB_ACCOUNT, posting_number)
        if job_id is not None:
            db.add(WbAssembly(job_id=job_id, print_after=print_after))
        else:
            job_id = db.scalar(select(Job.id).where(
                Job.kind == WB_ASSEMBLY_KIND, Job.account_key == WB_ACCOUNT,
                Job.posting_number == posting_number, Job.state.in_(ACTIVE),
            ))
            if job_id is None:
                raise HTTPException(409, "Перевод этого задания уже запрошен")
            if db.get(WbAssembly, job_id).print_after != print_after:
                raise HTTPException(409, "Для заказа уже выполняется другое действие")
    return RedirectResponse(f"/wb-assembly/{job_id}", status_code=303)


@app.get("/wb-assembly/{job_id}")
def wb_assembly_status(request: Request, job_id: UUID):
    with Session() as db:
        job = db.get(Job, job_id)
        action = db.get(WbAssembly, job_id) if job and job.kind == WB_ASSEMBLY_KIND else None
        if action is None:
            raise HTTPException(404, "Задание на перевод недоступно")
        order = db.get(Order, (WB_ACCOUNT, job.posting_number))
        task = db.get(PrintTask, action.print_task_id) if action.print_task_id else None
        batch = db.get(LabelBatch, task.batch_id) if task else None
        title = book_title(order) or (batch.labels[0].get("book_title") if batch and batch.labels else None)
        _, pending, failures = batch_versions(db, batch, LABEL_DIR) if batch else (0, 0, [])
    return templates.TemplateResponse(request, "wb_assembly_status.html", {
        "job": job, "action": action, "task": task, "title": title, "pending": pending, "failures": failures,
    })


def shift_confirmation(request: Request, *, error=None, status_code=200):
    with Session() as db:
        revision = db.get(Shift, 1).revision
    return templates.TemplateResponse(request, "shift_start.html", {
        "revision": revision, "error": error,
    }, status_code=status_code)


@app.get("/shift/start")
def shift_form(request: Request):
    return shift_confirmation(request)


@app.post("/shift/start")
def confirm_shift(request: Request, revision: Annotated[UUID, Form()]):
    if request.headers.get("origin") != "https://" + os.environ["PUBLIC_DOMAIN"]:
        raise HTTPException(403, "Откройте подтверждение начала смены в дашборде")
    try:
        new_revision = start_shift(revision)
    except ShiftConflict as error:
        return shift_confirmation(request, error=str(error), status_code=409)
    return RedirectResponse("/orders?" + urlencode({"shift_started": new_revision}), status_code=303)


def label_url(path: str, **options: bool) -> str:
    query = urlencode({name: "true" for name, enabled in options.items() if enabled})
    return f"{path}?{query}" if query else path


def pdf_bytes(writer: PdfWriter) -> bytes:
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def inline_pdf(source: bytes, filename: str):
    return Response(source, media_type="application/pdf",
                    headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(filename, safe='')}"})


def locked_print_task(db, task_id, token, state):
    task = db.get(PrintTask, task_id, with_for_update=True)
    if (task is None or task.lease_token != token or task.state != state
            or task.lease_until is None or task.lease_until <= now()):
        raise HTTPException(409, "Задание печати больше не закреплено за агентом")
    return task


@app.post("/api/agent/print-queue/claim")
def claim_print_job(auth: Agent):
    with write_transaction() as db:
        task = claim_print(db, LABEL_DIR)
        if task is None:
            return Response(status_code=204)
        return {"id": task.id, "lease_token": task.lease_token}


@app.get("/api/agent/print-queue/{task_id}/pdf")
def download_print_pdf(task_id: UUID, auth: Agent, token: Lease):
    with write_transaction() as db:
        task = locked_print_task(db, task_id, token, "leased")
        batch = db.get(LabelBatch, task.batch_id)
        try:
            data = pdf_bytes(batch_writer(db, batch, LABEL_DIR))
        except BatchTitleSpace as error:
            task.state = "failed"
            task.lease_until = None
            task.error = TITLE_SPACE_ERROR
            batch.labels = [
                {**item, "title_no_space": item["job_id"] in error.job_ids} for item in batch.labels
            ]
            return JSONResponse({"detail": task.error}, status_code=409)
        except (PyPdfError, PdfiumError, OSError, ValueError):
            task.state = "failed"
            task.lease_until = None
            task.error = "Не удалось подготовить PDF. Проверьте наличие этикеток и настройку названия книги."
            return JSONResponse({"detail": task.error}, status_code=409)
        batch.labels = label_metadata(db, batch, LABEL_DIR)
    return inline_pdf(data, "labels.pdf")


@app.post("/api/agent/print-queue/{task_id}/start", status_code=204)
def start_print(task_id: UUID, auth: Agent, token: Lease):
    with write_transaction() as db:
        task = locked_print_task(db, task_id, token, "leased")
        task.state = "sending"
        task.lease_until = now() + PRINT_LEASE
    return Response(status_code=204)


@app.post("/api/agent/print-queue/{task_id}/finish", status_code=204)
def finish_print(task_id: UUID, body: PrintResult, auth: Agent, token: Lease):
    with write_transaction() as db:
        task = locked_print_task(db, task_id, token, "sending")
        task.state = body.status
        task.lease_until = None
        if body.status == "failed":
            task.error = "Принтер не принял задание. Проверьте выбранную очередь macOS."
        elif body.status == "submitted":
            record_print(db, task, db.get(LabelBatch, task.batch_id), LABEL_DIR, submitted_at=now())
            mark_submitted_labels(db, task.id)
    return Response(status_code=204)


@app.get("/print-log")
def print_log(request: Request, page: Annotated[int, Query(ge=1, le=1_000_000)] = 1):
    page_size = 50
    with Session() as db:
        rows = db.scalars(select(PrintLog).order_by(
            PrintLog.event_at.desc(), PrintLog.task_id.desc(), PrintLog.label_job_id.desc(),
        ).offset((page - 1) * page_size).limit(page_size + 1)).all()
    return templates.TemplateResponse(request, "print_log.html", {
        "rows": rows[:page_size], "page": page, "has_next": len(rows) > page_size,
    })


@app.get("/print-jobs/{task_id}")
def print_job_status(request: Request, task_id: UUID):
    with Session() as db:
        task = db.get(PrintTask, task_id)
        if task is None:
            raise HTTPException(404, "Задание печати недоступно")
        batch = db.get(LabelBatch, task.batch_id)
        ready, pending, failures = batch_versions(db, batch, LABEL_DIR)
    state = task.state
    if state == "sending" and task.lease_until <= now():
        state = "unknown"
    return templates.TemplateResponse(request, "print_job.html", {
        "task": task, "batch": batch, "state": state, "ready": ready, "pending": pending, "failures": failures,
        "title_error": state == "failed" and task.error == TITLE_SPACE_ERROR,
        "title_postings": [item["posting_number"] for item in batch.labels
                           if item.get("title_no_space") and item.get("posting_number")],
        "originals_url": label_url(f"/label-batches/{batch.id}", originals=True),
    })


@app.post("/orders/{account_key}/{posting_number}/label-print/{state}")
def mark_label_print(request: Request, account_key: Account, posting_number: str,
                     state: Literal["printed", "unprinted"],
                     return_account: Annotated[AccountFilter | None, Form()] = None,
                     hide_printed: Annotated[bool, Form()] = False,
                     hide_waiting: Annotated[bool, Form()] = True):
    require_dashboard_origin(request)
    with write_transaction() as db:
        order = db.get(Order, (account_key, posting_number), with_for_update=True)
        if order is None or order.status not in LABEL_STATUSES:
            raise HTTPException(409, "Отметка о печати доступна после начала работы с этикеткой")
        if db.get(OrderWaiting, (account_key, posting_number)):
            raise HTTPException(409, "Сначала верните отправление из ожидания")
        if state == "printed":
            db.execute(insert(LabelPrint).values(
                account_key=account_key, posting_number=posting_number,
            ).on_conflict_do_nothing())
        else:
            db.execute(delete(LabelPrint).where(
                LabelPrint.account_key == account_key,
                LabelPrint.posting_number == posting_number,
            ))
    if return_account:
        return RedirectResponse(orders_url(return_account, hide_printed, hide_waiting), status_code=303)
    detail_url = request.url_for("detail", account_key=account_key, posting_number=posting_number)
    return RedirectResponse(detail_url.path, status_code=303)


@app.post("/orders/{account_key}/{posting_number}/waiting/{state}")
def change_order_waiting(request: Request, account_key: Account, posting_number: str,
                         state: Literal["hold", "release"],
                         return_account: Annotated[AccountFilter | None, Form()] = None,
                         hide_printed: Annotated[bool, Form()] = False,
                         hide_waiting: Annotated[bool, Form()] = True):
    require_dashboard_origin(request)
    with write_transaction() as db:
        order = db.get(Order, (account_key, posting_number), with_for_update=True)
        if order is None or order.status not in labels:
            raise HTTPException(409, "Отправление больше не активно")
        if state == "hold":
            if db.scalar(select(Job.id).where(
                Job.account_key == account_key, Job.posting_number == posting_number,
                Job.kind.in_((OZON_ASSEMBLY_KIND, WB_ASSEMBLY_KIND)), Job.state.in_(ACTIVE),
            ).limit(1)) is not None:
                raise HTTPException(409, "Дождитесь завершения сборки и повторите перенос")
            job_ids = {str(job_id) for job_id in db.scalars(select(Job.id).where(
                Job.account_key == account_key, Job.posting_number == posting_number, Job.kind == "label_pdf",
            ))}
            if job_ids:
                active_batches = db.scalars(select(LabelBatch.labels).join(PrintTask).where(
                    PrintTask.state.in_(("pending", "leased", "sending")),
                ))
                if any(item["job_id"] in job_ids for batch in active_batches for item in batch):
                    raise HTTPException(409, "Дождитесь завершения печати и повторите перенос")
            db.execute(insert(OrderWaiting).values(
                account_key=account_key, posting_number=posting_number,
            ).on_conflict_do_nothing())
        else:
            db.execute(delete(OrderWaiting).where(
                OrderWaiting.account_key == account_key, OrderWaiting.posting_number == posting_number,
            ))
    if return_account:
        return RedirectResponse(orders_url(return_account, hide_printed, hide_waiting), status_code=303)
    return RedirectResponse(request.url_for("detail", account_key=account_key, posting_number=posting_number).path,
                            status_code=303)


@app.post("/orders/{account_key}/{posting_number}/label")
def request_label(request: Request, account_key: Account, posting_number: str,
                  with_title: Annotated[bool, Form()] = False, autoprint: Annotated[bool, Form()] = False,
                  open_pdf: Annotated[bool, Form()] = False):
    require_dashboard_origin(request)
    with write_transaction() as db:
        order = db.get(Order, (account_key, posting_number), with_for_update=True)
        if order is None or order.status not in LABEL_STATUSES:
            raise HTTPException(409, "Этикетка недоступна на текущем этапе")
        if db.get(OrderWaiting, (account_key, posting_number)):
            raise HTTPException(409, "Сначала верните отправление из ожидания")
        job_id, ready = get_or_enqueue_label(db, order)
        if autoprint and request.headers.get("accept") != "application/json":
            task_id = queue_print(db, account_key, [batch_item(order, job_id, with_title)])
            return RedirectResponse(f"/print-jobs/{task_id}", status_code=303)
    if request.headers.get("accept") == "application/json":
        return JSONResponse({"status_url": f"/labels/{job_id}"}, status_code=200 if ready else 202)
    suffix = "/pdf" if ready else ""
    url = label_url(f"/labels/{job_id}{suffix}", with_title=with_title, autoprint=autoprint,
                    open_pdf=open_pdf and not ready)
    return RedirectResponse(url, status_code=303)


@app.post("/label-batches")
def request_label_batch(request: Request, account: Annotated[AccountFilter, Form()] = "all",
                        with_title: Annotated[bool, Form()] = False, autoprint: Annotated[bool, Form()] = False,
                        hide_printed: Annotated[bool, Form()] = False, show_waiting: Annotated[bool, Form()] = False):
    require_dashboard_origin(request)
    with write_transaction() as db:
        query = select(Order).where(Order.status.in_(LABEL_STATUSES))
        if account != "all":
            query = query.where(Order.account_key == account)
        if hide_printed:
            query = query.where(~has_printed_label())
        orders = db.scalars(query.order_by(Order.account_key, Order.posting_number).with_for_update()).all()
        waiting = set(db.execute(select(OrderWaiting.account_key, OrderWaiting.posting_number)))
        orders = [order for order in orders if (order.account_key, order.posting_number) not in waiting]
        if not orders:
            raise HTTPException(409, "Нет отправлений с доступными этикетками")
        items = []
        for order in orders:
            job_id, _ = get_or_enqueue_label(db, order)
            items.append(batch_item(order, job_id, with_title))
        if autoprint:
            task_id = queue_print(db, account, items)
            return RedirectResponse(f"/print-jobs/{task_id}", status_code=303)
        batch_id = create_batch(db, account, items)
    return RedirectResponse(label_url(f"/label-batches/{batch_id}", hide_printed=hide_printed,
                                      show_waiting=show_waiting), status_code=303)


@app.get("/label-batches/{batch_id}")
def label_batch(request: Request, batch_id: UUID, originals: bool = False, autoprint: bool = False,
                hide_printed: bool = False, show_waiting: bool = False):
    with Session() as db:
        batch = db.get(LabelBatch, batch_id)
        if batch is None or batch.expires_at <= now():
            raise HTTPException(404, "Партия этикеток недоступна")
        ready, pending, failures = batch_versions(db, batch, LABEL_DIR)
    context = {"batch": batch, "ready": ready, "failures": failures,
               "pending": pending, "autoprint": autoprint, "hide_printed": hide_printed, "show_waiting": show_waiting,
               "status_url": label_url(f"/label-batches/{batch_id}", autoprint=autoprint, hide_printed=hide_printed,
                                       show_waiting=show_waiting),
               "originals_url": label_url(f"/label-batches/{batch_id}", originals=True,
                                          autoprint=autoprint, hide_printed=hide_printed, show_waiting=show_waiting)}
    if pending or failures:
        return templates.TemplateResponse(request, "label_batch.html", context)
    try:
        with Session() as db:
            data = pdf_bytes(batch_writer(db, batch, LABEL_DIR, originals))
    except BatchTitleSpace as error:
        context["title_error"] = True
        context["title_postings"] = [item["posting_number"] for item in batch.labels
                                     if item["job_id"] in error.job_ids and item.get("posting_number")]
        return templates.TemplateResponse(request, "label_batch.html", context, status_code=409)
    except (PyPdfError, PdfiumError, OSError):
        context["merge_error"] = True
        context["can_open_originals"] = not originals and any(item["title"] for item in batch.labels)
        return templates.TemplateResponse(request, "label_batch.html", context, status_code=409)
    return inline_pdf(data, "labels.pdf")


@app.get("/labels/{job_id}")
def label_status(request: Request, job_id: UUID, with_title: bool = False, autoprint: bool = False,
                 open_pdf: bool = False):
    with Session() as db:
        job = db.get(Job, job_id)
    if job is None or job.kind != "label_pdf":
        raise HTTPException(404, "Этикетка не найдена")
    state = job.state
    if state == "ready" and (job.label_until <= now() or not pdf_is_available(job.id)):
        state = "expired"
    if request.headers.get("accept") == "application/json":
        return {
            "state": state, "error": job.error,
            "pdf_url": f"/labels/{job_id}/pdf" if state == "ready" else None,
            "label_number": label_number(job_id, job.account_key) if state == "ready" else None,
        }
    pdf_url = label_url(f"/labels/{job_id}/pdf", with_title=with_title, autoprint=autoprint)
    if state == "ready" and (autoprint or open_pdf):
        return RedirectResponse(pdf_url, status_code=303)
    return templates.TemplateResponse(request, "label.html", {
        "job": job, "state": state, "with_title": with_title, "autoprint": autoprint,
        "open_pdf": open_pdf, "pdf_url": pdf_url,
        "status_url": label_url(f"/labels/{job_id}", with_title=with_title, autoprint=autoprint, open_pdf=open_pdf),
    })


@app.get("/labels/{job_id}/pdf")
def label_pdf(request: Request, job_id: UUID, with_title: bool = False, autoprint: bool = False):
    with Session() as db:
        job = db.get(Job, job_id)
    path = LABEL_DIR / f"{job_id}.pdf"
    if not job or job.kind != "label_pdf" or job.state != "ready" or job.label_until <= now() or not path.is_file():
        raise HTTPException(404, "Этикетка недоступна")
    marketplace = "wb" if job.account_key == WB_ACCOUNT else "ozon"
    filename = f"{marketplace}-{job.posting_number}.pdf"
    if not with_title:
        return FileResponse(path, media_type="application/pdf", filename=filename, content_disposition_type="inline")

    with Session() as db:
        order = db.get(Order, (job.account_key, job.posting_number))
    title = book_title(order)
    if title:
        try:
            data = add_title(path.read_bytes(), title, wb=job.account_key == WB_ACCOUNT)
            return inline_pdf(data, filename)
        except (NoTitleSpace, PyPdfError, PdfiumError, OSError):
            error = "Не удалось разместить название внизу этикетки. Откройте оригинальный PDF."
    else:
        error = "В отправлении нет названия книги."
    return templates.TemplateResponse(request, "label.html", {
        "job": job, "state": "ready", "title_error": error, "with_title": False,
        "pdf_url": label_url(f"/labels/{job_id}/pdf", autoprint=autoprint),
    }, status_code=409)
