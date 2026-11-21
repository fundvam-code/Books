from datetime import UTC, datetime, timedelta

import httpx

from .errors import MarketplaceError as OzonError

ORIGIN = "https://api-seller.ozon.ru"
ACTIVE = ("awaiting_packaging", "awaiting_deliver")
PDF_LIMIT = 8 * 1024 * 1024


class Ozon:
    def __init__(self, client, client_id, api_key):
        self.client = client
        self.headers = {"Client-Id": client_id, "Api-Key": api_key}

    async def post(self, path, body, *, label=False):
        try:
            response = await self.client.post(ORIGIN + path, json=body, headers=self.headers)
        except httpx.RequestError as error:
            raise OzonError("unavailable", True) from error
        code = response.status_code
        if code == 429:
            after = response.headers.get("retry-after", "")
            raise OzonError("rate", True, min(int(after), 3600) if after.isdecimal() and len(after) < 8 else None)
        if code >= 500 or code in (408, 425):
            raise OzonError("unavailable", True)
        if code in (401, 403):
            raise OzonError("access")
        if label and code == 409:
            raise OzonError("label", True)
        if code >= 400:
            raise OzonError("label" if label else "response")
        data = response.json()
        result = data.get("result")
        if isinstance(result, dict):
            return {**data, **result}
        return data

    async def snapshot(self, key, known):
        end = datetime.now(UTC)
        start = end - timedelta(days=360)
        orders = {}
        cursor = ""
        seen = set()
        while True:
            page = await self.post("/v4/posting/fbs/list", {
                "filter": {"since": start.isoformat(), "to": end.isoformat(), "statuses": list(ACTIVE)},
                "cursor": cursor, "limit": 100, "sort_dir": "ASC", "with": {"analytics_data": True},
            })
            postings = page["postings"]
            if not isinstance(postings, list) or not isinstance(page["has_next"], bool):
                raise OzonError("response", True)
            for row in postings:
                if row["status"] in ACTIVE:
                    orders[row["posting_number"]] = normalize(key, row)
            if page["has_next"] is False:
                break
            cursor = page["cursor"]
            if not cursor or cursor in seen:
                raise OzonError("response", True)
            seen.add(cursor)

        for row in known:
            number = row["posting_number"]
            if number in orders:
                continue
            created = row.get("created_at")
            if created:
                stamp = datetime.fromisoformat(created)
                if stamp.tzinfo is not None and start <= stamp <= end:
                    continue
            posting = await self.post("/v3/posting/fbs/get", {
                "posting_number": number, "with": {"analytics_data": True},
            })
            if posting["status"] in ACTIVE:
                orders[number] = normalize(key, posting)
        return {"orders": list(orders.values()), "covered_since": start.isoformat()}

    async def create_label(self, number):
        reply = await self.post("/v3/posting/fbs/package-label/create", {"posting_numbers": [number]}, label=True)
        tasks = reply["tasks"]
        preferred = next((task for task in tasks if task.get("task_type") == "small_label"), None)
        if preferred is None:
            preferred = next(iter(tasks), None)
        if preferred is None:
            raise OzonError("label", True)
        return int(preferred["task_id"])

    async def get_label_url(self, task_id):
        reply = await self.post("/v2/posting/fbs/package-label/get", {"task_id": task_id}, label=True)
        status = reply.get("status")
        details = status if isinstance(status, dict) else reply
        code = details.get("code") if isinstance(status, dict) else status
        if code in ("pending", "in_progress"):
            return None
        file_url = reply.get("file_url")
        if (
            code != "completed" or not isinstance(file_url, str) or not file_url
            or details.get("unprinted_postings") or details.get("printed_postings_count") == 0
        ):
            raise OzonError("label")
        return file_url

    async def fetch_pdf(self, url):
        target = httpx.URL(url)
        if target.scheme != "https" or not target.host or target.userinfo:
            raise OzonError("label")
        try:
            async with self.client.stream("GET", target) as response:
                if response.status_code != 200:
                    raise OzonError("label", True)
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > PDF_LIMIT:
                        raise OzonError("label")
                    data.extend(chunk)
        except httpx.RequestError as error:
            raise OzonError("unavailable", True) from error
        if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-2048:]:
            raise OzonError("label")
        return bytes(data)


def value_text(value):
    return "" if value is None else str(value)


def format_price(value, currency):
    if isinstance(value, dict):
        return " ".join(filter(None, (value_text(value.get("amount", value.get("value"))), value_text(value.get("currency", value.get("currency_code")))))) or None
    return None if value is None else " ".join(filter(None, (str(value), value_text(currency))))


def normalize(key, row):
    analytics = row.get("analytics_data") or {}
    delivery = row.get("delivery_method") or {}
    return {
        "account_key": key,
        "posting_number": row["posting_number"],
        "order_number": row.get("order_number"),
        "status": row["status"],
        "created_at": row.get("created_at") or row.get("in_process_at") or None,
        "shipment_date": row.get("shipment_date") or None,
        "warehouse": delivery.get("warehouse") or analytics.get("warehouse") or analytics.get("warehouse_name"),
        "delivery_method": delivery.get("name"),
        "products": [{
            "name": value_text(product.get("name")),
            "offer_id": value_text(product.get("offer_id")),
            "sku": value_text(product.get("sku")),
            "quantity": int(product["quantity"]),
            "price": format_price(product.get("price"), product.get("currency_code")),
        } for product in row["products"]],
    }
