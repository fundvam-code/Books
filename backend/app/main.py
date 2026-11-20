import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from pathlib import Path
from secrets import compare_digest
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert

from .db import Cabinet, Heartbeat, Job, Order, Session
from .jobs import ACCOUNTS, ACTIVE, PDF_LIMIT, STATUSES, apply_snapshot, claim, enqueue, fail_job, initialize, maintain, now
from .schemas import Account, AgentHeartbeat, Completion, LabelTask

ROOT = Path(__file__).parent
LABEL_DIR = Path(os.environ.get("LABEL_DIR", "/labels"))
templates = Jinja2Templates(directory=ROOT / "templates")
labels = dict(zip(STATUSES, ("Ожидают сборки", "Собраны — ждут отгрузки")))


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


@app.get("/orders")
def orders(request: Request, account: str = "all"):
    if account not in ("all", *ACCOUNTS):
        raise HTTPException(404, "Кабинет не найден")
    keys = ACCOUNTS if account == "all" else (account,)
    with Session() as db:
        cabinets = list(db.scalars(select(Cabinet).where(Cabinet.key.in_(keys)).order_by(Cabinet.key)))
        seen = db.get(Heartbeat, 1)
        records = list(db.scalars(select(Order).where(Order.account_key.in_(keys))))
    records.sort(key=lambda order: (order.data.get("shipment_date") or "~", order.posting_number))
    columns = [(labels[status], [order for order in records if order.status == status]) for status in STATUSES]
    return templates.TemplateResponse(request, "orders.html", {
        "account": account, "cabinets": cabinets, "columns": columns,
        "online": bool(seen and seen.seen_at >= now() - timedelta(seconds=120)),
    })


@app.get("/orders/{account_key}/{posting_number}")
def detail(request: Request, account_key: Account, posting_number: str):
    with Session() as db:
        order = db.get(Order, (account_key, posting_number))
    if order is None:
        raise HTTPException(404, "Активное отправление не найдено")
    return templates.TemplateResponse(request, "detail.html", {
        "order": order, "data": order.data, "status_label": labels[order.status],
    })


@app.post("/orders/{account_key}/{posting_number}/label")
def request_label(request: Request, account_key: Account, posting_number: str):
    if request.headers.get("origin") != "https://" + os.environ["PUBLIC_DOMAIN"]:
        raise HTTPException(403, "Откройте форму печати в дашборде")
    with Session.begin() as db:
        order = db.get(Order, (account_key, posting_number), with_for_update=True)
        if order is None or order.status != "awaiting_deliver":
            raise HTTPException(409, "Этикетка доступна для собранного отправления")
        query = select(Job).where(
            Job.kind == "label_pdf", Job.account_key == account_key, Job.posting_number == posting_number,
        )
        job = db.scalar(query.where(Job.state == "ready", Job.label_until > now())
                        .order_by(desc(Job.label_until)).limit(1))
        if job:
            return RedirectResponse(f"/labels/{job.id}/pdf", status_code=303)
        job = db.scalar(query.where(Job.state.in_(ACTIVE)))
        job_id = job.id if job else enqueue(db, "label_pdf", account_key, posting_number)
    return RedirectResponse(f"/labels/{job_id}", status_code=303)


@app.get("/labels/{job_id}")
def label_status(request: Request, job_id: UUID):
    with Session() as db:
        job = db.get(Job, job_id)
    if job is None or job.kind != "label_pdf":
        raise HTTPException(404, "Этикетка не найдена")
    state = job.state
    if state == "ready" and job.label_until <= now():
        state = "expired"
    return templates.TemplateResponse(request, "label.html", {"job": job, "state": state})


@app.get("/labels/{job_id}/pdf")
def label_pdf(job_id: UUID):
    with Session() as db:
        job = db.get(Job, job_id)
    path = LABEL_DIR / f"{job_id}.pdf"
    if not job or job.state != "ready" or job.label_until <= now() or not path.is_file():
        raise HTTPException(404, "Этикетка недоступна")
    return FileResponse(path, media_type="application/pdf", filename=f"ozon-{job.posting_number}.pdf",
                        content_disposition_type="inline")
