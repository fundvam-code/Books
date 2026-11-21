import asyncio
import logging
import subprocess

import httpx

LP = "/usr/bin/lp"
log = logging.getLogger("agent")


def submit_pdf(pdf, printer, task_id):
    args = [LP, "-n", "1", "-o", "print-scaling=none", "-t", f"FBS labels {task_id}"]
    if printer != "default":
        args.extend(("-d", printer))
    try:
        result = subprocess.run([*args, "-"], input=pdf, capture_output=True, timeout=60, check=False)
    except subprocess.TimeoutExpired:
        return "unknown"
    except OSError:
        return "failed"
    return "submitted" if result.returncode == 0 else "failed"


async def print_task(server, printer, task):
    task_id = task["id"]
    route = f"/print-queue/{task_id}"
    headers = {"X-Lease-Token": task["lease_token"]}
    response = await server.client.get("/api/agent" + route + "/pdf", headers=headers, timeout=90)
    response.raise_for_status()
    if response.headers.get("content-type") != "application/pdf" or not response.content.startswith(b"%PDF-"):
        await server.request("POST", route + "/start", headers=headers)
        await server.request("POST", route + "/finish", headers=headers, json={"status": "failed"})
        return
    await server.request("POST", route + "/start", headers=headers)
    status = await asyncio.to_thread(submit_pdf, response.content, printer, task_id)
    await server.request("POST", route + "/finish", headers=headers, json={"status": status})
    if status == "submitted":
        log.info("PDF передан в очередь принтера")
    else:
        log.warning("Отправка PDF завершилась со статусом %s", status)


async def print_worker(server, printer):
    delay = 2
    while True:
        try:
            task = await server.request("POST", "/print-queue/claim")
            delay = 2
            if task:
                await print_task(server, printer, task)
                continue
        except httpx.HTTPError:
            log.warning("Не удалось обработать запрос печати")
            delay = min(delay * 2, 30)
        await asyncio.sleep(delay)
