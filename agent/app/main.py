import asyncio
import logging
import os
from datetime import UTC, datetime

import httpx

from .ozon import Ozon, OzonError

log = logging.getLogger("agent")


class Server:
    def __init__(self, client):
        self.client = client

    async def request(self, method, path, *, job=None, **kwargs):
        headers = kwargs.pop("headers", {})
        if job:
            headers["X-Lease-Token"] = job["lease_token"]
            path = f'/jobs/{job["id"]}/{path}'
        response = await self.client.request(method, "/api/agent" + path, headers=headers, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else None


async def heartbeat(server, profiles):
    while True:
        try:
            await server.request("POST", "/heartbeat", json={
                "agent_version": "0.1.0", "account_keys": list(profiles),
                "observed_at": datetime.now(UTC).isoformat(),
            })
        except httpx.HTTPError:
            log.warning("Не удалось передать состояние агента")
        await asyncio.sleep(30)


async def renew(server, job, worker):
    while True:
        await asyncio.sleep(30)
        try:
            await server.request("POST", "renew", job=job)
        except httpx.HTTPError:
            worker.cancel()
            return


async def process(server, job, profiles):
    ozon = profiles.get(job["account_key"])
    payload = job["payload"]
    try:
        if ozon is None:
            raise OzonError("profile")
        if job["kind"] == "sync_orders":
            result = await ozon.snapshot(job["account_key"], payload["known_postings"])
            await server.request("POST", "complete", job=job, json={"status": "succeeded", "result": result})
            return

        task_id = payload["ozon_task_id"]
        if task_id is None:
            task_id = await ozon.create_label(payload["posting_number"])
            await server.request("PUT", "label-task", job=job, json={"ozon_task_id": task_id})
        for _ in range(12):
            url = await ozon.get_label_url(task_id)
            if url:
                data = await ozon.fetch_pdf(url)
                await server.request("PUT", "label", job=job, content=data,
                                     headers={"Content-Type": "application/pdf"})
                return
            await asyncio.sleep(5)
        raise OzonError("label", True)
    except (KeyError, TypeError, ValueError):
        error = OzonError("response")
    except OzonError as failure:
        error = failure
    await server.request("POST", "complete", job=job, json={
        "status": "retryable_error" if error.retry else "permanent_error",
        "error": error.code, "retry_after": error.retry_after,
    })


async def run():
    url = httpx.URL(os.environ.get("DASHBOARD_URL", ""))
    secret = os.environ.get("AGENT_SHARED_SECRET", "")
    if url.scheme != "https" or not url.host or url.userinfo or url.path != "/" or url.query or url.fragment or len(secret) < 32:
        raise SystemExit("Укажите HTTPS-адрес дашборда и секрет агента длиной от 32 символов")
    keys = {key: (os.environ.get(f"OZON_CLIENT_ID_{key}"), os.environ.get(f"OZON_API_KEY_{key}"))
            for key in ("1", "2")}
    async with (
        httpx.AsyncClient(base_url=url, headers={"Authorization": f"Bearer {secret}"}, timeout=15, trust_env=False) as api,
        httpx.AsyncClient(timeout=20, trust_env=False) as ozon,
    ):
        profiles = {key: Ozon(ozon, client_id, api_key) for key, (client_id, api_key) in keys.items()
                    if client_id and api_key}
        if not profiles:
            raise SystemExit("Настройте ключ хотя бы одного кабинета Ozon")
        server = Server(api)
        async with asyncio.TaskGroup() as group:
            group.create_task(heartbeat(server, profiles))
            delay = 2
            while True:
                try:
                    job = await server.request("POST", "/jobs/claim")
                    delay = 2
                    if job is None:
                        await asyncio.sleep(delay)
                        continue
                    worker = asyncio.create_task(process(server, job, profiles))
                    lease = asyncio.create_task(renew(server, job, worker))
                    try:
                        await worker
                    except asyncio.CancelledError:
                        if asyncio.current_task().cancelling():
                            raise
                        log.warning("Потеряна аренда задания")
                    finally:
                        lease.cancel()
                        await asyncio.gather(lease, return_exceptions=True)
                except httpx.HTTPError:
                    log.warning("Сервер дашборда не принял запрос агента")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run())
