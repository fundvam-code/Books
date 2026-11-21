import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta, timezone
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from secrets import compare_digest
from typing import Annotated, Literal
from urllib.parse import quote, urlencode
from uuid import UUID

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PyPdfError
from pypdfium2 import PdfiumError
from sqlalchemy import and_, delete, desc, select
from sqlalchemy.dialects.postgresql import insert

from .db import Cabinet, Heartbeat, Job, LabelBatch, LabelPrint, Order, PrintTask, Session
from .jobs import ACCOUNTS, ACTIVE, PDF_LIMIT, STATUSES, apply_snapshot, claim, enqueue, fail_job, initialize, maintain, now
from .label_title import NoTitleSpace, add_title
from .printing import PRINT_LEASE, batch_versions, batch_writer, claim_print, create_batch, queue_print
from .schemas import Account, AgentHeartbeat, Completion, LabelTask, PrintResult

ROOT = Path(__file__).parent
LABEL_DIR = Path(os.environ.get("LABEL_DIR", "/labels"))
MSK = timezone(timedelta(hours=3), "MSK")
templates = Jinja2Templates(directory=ROOT / "templates")
templates.env.globals["cabinet_names"] = {"1": "Золотая антилопа", "2": "Зеленая книга"}
labels = dict(zip(STATUSES, ("Ожидают сборки", "Собраны — ждут отгрузки")))


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


templates.env.filters["msk"] = format_msk


def orders_url(account: str, hide_printed: bool = False) -> str:
    options = {"account": account}
    if hide_printed:
        options["hide_printed"] = "true"
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
    with Session.begin() as db:
        stamp = now()
        db.execute(insert(Heartbeat).values(id=1, seen_at=stamp).on_conflict_do_update(
            index_elements=["id"], set_={"seen_at": stamp},
        ))
    return Response(status_code=204)


@app.post("/api/agent/jobs/claim")
def claim_job(auth: Agent):
    with Session.begin() as db:
        job = claim(db)
        if job is None:
            return Response(status_code=204)
        payload = {}
        if job.kind == "label_pdf":
            payload = {"posting_number": job.posting_number, "ozon_task_id": job.ozon_task_id}
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
    with Session.begin() as db:
        job = locked_job(db, job_id, token)
        job.lease_until = now() + timedelta(seconds=90)
    return Response(status_code=204)


@app.post("/api/agent/jobs/{job_id}/complete", status_code=204)
def complete_job(job_id: UUID, body: Completion, auth: Agent, token: Lease):
    with Session.begin() as db:
        job = locked_job(db, job_id, token)
        if body.status == "succeeded":
            if job.kind != "sync_orders" or body.result is None:
                raise HTTPException(422, "Ожидался снимок заказов")
            try:
                apply_snapshot(db, job.account_key, body.result)
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
            job.state = "done"
            job.lease_until = None
        else:
            fail_job(db, job, body)
    return Response(status_code=204)


@app.put("/api/agent/jobs/{job_id}/label-task", status_code=204)
def save_label_task(job_id: UUID, body: LabelTask, auth: Agent, token: Lease):
    with Session.begin() as db:
        job = locked_job(db, job_id, token)
        if job.kind != "label_pdf" or job.ozon_task_id not in (None, body.ozon_task_id):
            raise HTTPException(409, "Идентификатор задания уже закреплён")
        job.ozon_task_id = body.ozon_task_id
    return Response(status_code=204)


def publish_pdf(job_id, token, data):
    with Session.begin() as db:
        job = locked_job(db, job_id, token, allow_ready=True)
        if job.state == "ready":
            return
        if job.kind != "label_pdf" or job.ozon_task_id is None:
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


@app.put("/api/agent/jobs/{job_id}/label", status_code=204)
async def upload_pdf(job_id: UUID, request: Request, auth: Agent, token: Lease):
    if request.headers.get("content-type") != "application/pdf":
        raise HTTPException(415, "Ожидался PDF")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > PDF_LIMIT:
            raise HTTPException(413, "PDF превышает 8 МБ")
        data.extend(chunk)
    if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-2048:]:
        raise HTTPException(422, "Некорректный PDF")
    await asyncio.to_thread(publish_pdf, job_id, token, data)
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
    ).outerjoin(LabelPrint, and_(
        LabelPrint.account_key == Order.account_key,
        LabelPrint.posting_number == Order.posting_number,
    ))


def pdf_is_available(job_id):
    return job_id is not None and (LABEL_DIR / f"{job_id}.pdf").is_file()


@lru_cache(maxsize=512)
def label_number(job_id: UUID | None) -> str | None:
    if job_id is None:
        return None
    try:
        with (LABEL_DIR / f"{job_id}.pdf").open("rb") as source:
            numbers = (
                f"{prefix} {suffix}"
                for page in PdfReader(source).pages
                for prefix, suffix in re.findall(r"(?mi)^\s*(ii[0-9]{7})\s+([0-9]{4})[ \t]*$", page.extract_text())
            )
            return ", ".join(dict.fromkeys(numbers)) or None
    except (OSError, PyPdfError, ValueError):
        return None


templates.env.globals["label_number"] = label_number


def book_title(order):
    products = order.data.get("products", []) if order else []
    names = (product.get("name") or "" for product in products)
    return ", ".join(dict.fromkeys(name for name in (" ".join(text.split()) for text in names) if name))


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
def orders(request: Request, account: str = "all", hide_printed: bool = False):
    if account not in ("all", *ACCOUNTS):
        raise HTTPException(404, "Кабинет не найден")
    keys = ACCOUNTS if account == "all" else (account,)
    with Session() as db:
        cabinets = list(db.scalars(select(Cabinet).where(Cabinet.key.in_(keys)).order_by(Cabinet.key)))
        seen = db.get(Heartbeat, 1)
        query = orders_with_labels().where(Order.account_key.in_(keys))
        if hide_printed:
            query = query.where(LabelPrint.posting_number.is_(None))
        records = db.execute(query).all()
    records.sort(key=lambda row: (row[0].data.get("shipment_date") or "~", row[0].posting_number))
    columns = [
        (labels[status], [(order, printed, job_id if pdf_is_available(job_id) else None)
                         for order, printed, job_id in records if order.status == status])
        for status in STATUSES
    ]
    return templates.TemplateResponse(request, "orders.html", {
        "account": account, "cabinets": cabinets, "columns": columns,
        "hide_printed": hide_printed,
        "has_assembled": any(order.status == "awaiting_deliver" for order, _, _ in records),
        "online": bool(seen and seen.seen_at >= now() - timedelta(seconds=120)),
    })


@app.get("/orders/{account_key}/{posting_number}")
def detail(request: Request, account_key: Account, posting_number: str):
    with Session() as db:
        row = db.execute(orders_with_labels().where(
            Order.account_key == account_key, Order.posting_number == posting_number,
        )).one_or_none()
    if row is None:
        raise HTTPException(404, "Активное отправление не найдено")
    order, printed, job_id = row
    return templates.TemplateResponse(request, "detail.html", {
        "order": order, "data": order.data, "status_label": labels[order.status],
        "printed": printed, "label_job_id": job_id if pdf_is_available(job_id) else None,
    })


def require_dashboard_origin(request: Request):
    if request.headers.get("origin") != "https://" + os.environ["PUBLIC_DOMAIN"]:
        raise HTTPException(403, "Откройте форму печати в дашборде")


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
    with Session.begin() as db:
        task = claim_print(db, LABEL_DIR)
        if task is None:
            return Response(status_code=204)
        return {"id": task.id, "lease_token": task.lease_token}


@app.get("/api/agent/print-queue/{task_id}/pdf")
def download_print_pdf(task_id: UUID, auth: Agent, token: Lease):
    with Session.begin() as db:
        task = locked_print_task(db, task_id, token, "leased")
        batch = db.get(LabelBatch, task.batch_id)
        try:
            data = pdf_bytes(batch_writer(batch, LABEL_DIR))
        except (NoTitleSpace, PyPdfError, PdfiumError, OSError, ValueError):
            task.state = "failed"
            task.lease_until = None
            task.error = "Не удалось подготовить PDF. Проверьте наличие этикеток и настройку названия книги."
            return JSONResponse({"detail": task.error}, status_code=409)
    return inline_pdf(data, "ozon-labels.pdf")


@app.post("/api/agent/print-queue/{task_id}/start", status_code=204)
def start_print(task_id: UUID, auth: Agent, token: Lease):
    with Session.begin() as db:
        task = locked_print_task(db, task_id, token, "leased")
        task.state = "sending"
        task.lease_until = now() + PRINT_LEASE
    return Response(status_code=204)


@app.post("/api/agent/print-queue/{task_id}/finish", status_code=204)
def finish_print(task_id: UUID, body: PrintResult, auth: Agent, token: Lease):
    with Session.begin() as db:
        task = locked_print_task(db, task_id, token, "sending")
        task.state = body.status
        task.lease_until = None
        if body.status == "failed":
            task.error = "Принтер не принял задание. Проверьте выбранную очередь macOS."
    return Response(status_code=204)


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
    })


@app.post("/orders/{account_key}/{posting_number}/label-print/{state}")
def mark_label_print(request: Request, account_key: Account, posting_number: str,
                     state: Literal["printed", "unprinted"],
                     return_account: Annotated[Literal["all", "1", "2"] | None, Form()] = None,
                     hide_printed: Annotated[bool, Form()] = False):
    require_dashboard_origin(request)
    with Session.begin() as db:
        order = db.get(Order, (account_key, posting_number), with_for_update=True)
        if order is None or order.status != "awaiting_deliver":
            raise HTTPException(409, "Отметка о печати доступна для собранного отправления")
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
        return RedirectResponse(orders_url(return_account, hide_printed), status_code=303)
    detail_url = request.url_for("detail", account_key=account_key, posting_number=posting_number)
    return RedirectResponse(detail_url.path, status_code=303)


@app.post("/orders/{account_key}/{posting_number}/label")
def request_label(request: Request, account_key: Account, posting_number: str,
                  with_title: Annotated[bool, Form()] = False, autoprint: Annotated[bool, Form()] = False,
                  open_pdf: Annotated[bool, Form()] = False):
    require_dashboard_origin(request)
    with Session.begin() as db:
        order = db.get(Order, (account_key, posting_number), with_for_update=True)
        if order is None or order.status != "awaiting_deliver":
            raise HTTPException(409, "Этикетка доступна для собранного отправления")
        job_id, ready = get_or_enqueue_label(db, order)
        if autoprint and request.headers.get("accept") != "application/json":
            task_id = queue_print(db, account_key, [
                {"job_id": str(job_id), "title": book_title(order) if with_title else ""},
            ])
            return RedirectResponse(f"/print-jobs/{task_id}", status_code=303)
    if request.headers.get("accept") == "application/json":
        return JSONResponse({"status_url": f"/labels/{job_id}"}, status_code=200 if ready else 202)
    suffix = "/pdf" if ready else ""
    url = label_url(f"/labels/{job_id}{suffix}", with_title=with_title, autoprint=autoprint,
                    open_pdf=open_pdf and not ready)
    return RedirectResponse(url, status_code=303)


@app.post("/label-batches")
def request_label_batch(request: Request, account: Annotated[Literal["all", "1", "2"], Form()] = "all",
                        with_title: Annotated[bool, Form()] = False, autoprint: Annotated[bool, Form()] = False,
                        hide_printed: Annotated[bool, Form()] = False):
    require_dashboard_origin(request)
    keys = ACCOUNTS if account == "all" else (account,)
    with Session.begin() as db:
        query = select(Order).where(
            Order.account_key.in_(keys), Order.status == "awaiting_deliver",
        )
        if hide_printed:
            printed = select(LabelPrint.posting_number).where(
                LabelPrint.account_key == Order.account_key, LabelPrint.posting_number == Order.posting_number,
            ).exists()
            query = query.where(~printed)
        orders = db.scalars(query.order_by(Order.account_key, Order.posting_number).with_for_update()).all()
        if not orders:
            raise HTTPException(409, "Нет собранных отправлений для печати")
        items = []
        for order in orders:
            job_id, _ = get_or_enqueue_label(db, order)
            items.append({"job_id": str(job_id), "title": book_title(order) if with_title else ""})
        if autoprint:
            task_id = queue_print(db, account, items)
            return RedirectResponse(f"/print-jobs/{task_id}", status_code=303)
        batch_id = create_batch(db, account, items)
    return RedirectResponse(label_url(f"/label-batches/{batch_id}", hide_printed=hide_printed), status_code=303)


@app.get("/label-batches/{batch_id}")
def label_batch(request: Request, batch_id: UUID, originals: bool = False, autoprint: bool = False,
                hide_printed: bool = False):
    with Session() as db:
        batch = db.get(LabelBatch, batch_id)
        if batch is None or batch.expires_at <= now():
            raise HTTPException(404, "Партия этикеток недоступна")
        ready, pending, failures = batch_versions(db, batch, LABEL_DIR)
    context = {"batch": batch, "ready": ready, "failures": failures,
               "pending": pending, "autoprint": autoprint, "hide_printed": hide_printed,
               "status_url": label_url(f"/label-batches/{batch_id}", autoprint=autoprint, hide_printed=hide_printed),
               "originals_url": label_url(f"/label-batches/{batch_id}", originals=True,
                                          autoprint=autoprint, hide_printed=hide_printed)}
    if pending or failures:
        return templates.TemplateResponse(request, "label_batch.html", context)
    try:
        data = pdf_bytes(batch_writer(batch, LABEL_DIR, originals))
    except (NoTitleSpace, PyPdfError, PdfiumError, OSError):
        context["merge_error"] = True
        context["can_open_originals"] = not originals and any(item["title"] for item in batch.labels)
        return templates.TemplateResponse(request, "label_batch.html", context, status_code=409)
    return inline_pdf(data, "ozon-labels.pdf")


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
            "label_number": label_number(job_id) if state == "ready" else None,
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
    filename = f"ozon-{job.posting_number}.pdf"
    if not with_title:
        return FileResponse(path, media_type="application/pdf", filename=filename, content_disposition_type="inline")

    with Session() as db:
        order = db.get(Order, (job.account_key, job.posting_number))
    title = book_title(order)
    if title:
        try:
            data = add_title(path.read_bytes(), title)
            return inline_pdf(data, filename)
        except (NoTitleSpace, PyPdfError, PdfiumError, OSError):
            error = "Не удалось разместить название внизу этикетки. Откройте оригинальный PDF."
    else:
        error = "В отправлении нет названия книги."
    return templates.TemplateResponse(request, "label.html", {
        "job": job, "state": "ready", "title_error": error, "with_title": False,
        "pdf_url": label_url(f"/labels/{job_id}/pdf", autoprint=autoprint),
    }, status_code=409)
