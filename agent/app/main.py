import asyncio
import logging
import os
import sys
from datetime import UTC, datetime

import httpx

from .errors import MarketplaceError
from .ozon import Ozon
from .printing import LP, print_worker
from .wildberries import ACCOUNT as WB_ACCOUNT, Wildberries

OZON_ASSEMBLY_KIND = "ozon_assembly"

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


async def heartbeat(server, capabilities):
    while True:
        try:
            await server.request("POST", "/heartbeat", json={
                "agent_version": "0.1.0", **capabilities,
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
    account = job["account_key"]
    marketplace = profiles.get(account)
    payload = job["payload"]
    try:
        if marketplace is None:
            raise MarketplaceError("profile")
        if job["kind"] == "sync_orders":
            result = await marketplace.snapshot(account, payload["known_postings"])
            await server.request("POST", "complete", job=job, json={"status": "succeeded", "result": result})
            return

        if job["kind"] == OZON_ASSEMBLY_KIND:
            await server.request("POST", "assembly-start", job=job)
            status = await marketplace.assemble(payload["posting_number"], payload["products"])
            await server.request("POST", "complete", job=job, json={
                "status": "succeeded", "assembly_status": status,
            })
            return

        if account == WB_ACCOUNT and job["kind"] == "wb_assembly":
            number = payload["posting_number"]
            supply = payload.get("supply_id") or await marketplace.find_open_supply()
            await marketplace.check_assembly(number, supply)
            await server.request("POST", "assembly-start", job=job, json={"supply_id": supply})
            await marketplace.move_to_assembly(number, supply)
            await server.request("POST", "complete", job=job, json={"status": "succeeded"})
            return

        if account == WB_ACCOUNT:
            data, number = await marketplace.get_sticker(payload["posting_number"])
            try:
                await server.request("PUT", "wb-sticker", job=job, content=data,
                                     headers={"Content-Type": "image/png", "X-Label-Number": number})
            except httpx.HTTPStatusError as error:
                if error.response.status_code not in (413, 415, 422):
                    raise
                raise MarketplaceError("label") from error
            return

        task_id = payload["ozon_task_id"]
        if task_id is None:
            task_id = await marketplace.create_label(payload["posting_number"])
            await server.request("PUT", "label-task", job=job, json={"ozon_task_id": task_id})
        for _ in range(12):
            url = await marketplace.get_label_url(task_id)
            if url:
                data = await marketplace.fetch_pdf(url)
                await server.request("PUT", "label", job=job, content=data,
                                     headers={"Content-Type": "application/pdf"})
                return
            await asyncio.sleep(5)
        raise MarketplaceError("label", True)
    except (KeyError, TypeError, ValueError):
        error = MarketplaceError("response")
    except MarketplaceError as failure:
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
    printer = os.environ.get("OZON_PRINTER", "").strip()
    if printer and (sys.platform != "darwin" or not os.access(LP, os.X_OK)):
        raise SystemExit("Для печати запустите агент нативно на macOS")
    keys = {key: (os.environ.get(f"OZON_CLIENT_ID_{key}"), os.environ.get(f"OZON_API_KEY_{key}"))
            for key in ("1", "2")}
    async with (
        httpx.AsyncClient(base_url=url, headers={"Authorization": f"Bearer {secret}"}, timeout=15, trust_env=False) as api,
        httpx.AsyncClient(timeout=20, trust_env=False) as marketplace,
    ):
        profiles = {key: Ozon(marketplace, client_id, api_key) for key, (client_id, api_key) in keys.items()
                    if client_id and api_key}
        wb_token = os.environ.get("WB_API_TOKEN", "").strip()
        wb_write_token = os.environ.get("WB_WRITE_TOKEN", "").strip()
        if wb_token:
            profiles[WB_ACCOUNT] = Wildberries(
                marketplace, wb_token, os.environ.get("WB_CONTENT_TOKEN", "").strip(), wb_write_token,
            )
        if not profiles:
            raise SystemExit("Настройте ключ хотя бы одного кабинета Ozon или Wildberries")
        server = Server(api)
        wb_assembly = bool(wb_token and wb_write_token)
        ozon_accounts = [key for key in profiles if key in ("1", "2")]
        capabilities = {
            "account_keys": list(profiles), "wb_assembly": wb_assembly, "wb_auto_assembly": wb_assembly,
            "ozon_assembly_accounts": ozon_accounts,
            "ozon_print_accounts": ozon_accounts if printer else [],
        }
        async with asyncio.TaskGroup() as group:
            group.create_task(heartbeat(server, capabilities))
            if printer:
                group.create_task(print_worker(server, printer))
            delay = 2
            while True:
                try:
                    job = await server.request("POST", "/jobs/claim", json=capabilities)
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
