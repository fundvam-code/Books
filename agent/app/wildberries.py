import asyncio
import logging
import re
from base64 import b64decode
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import ceil
from time import monotonic

import httpx

from .errors import MarketplaceError

MARKETPLACE = "https://marketplace-api.wildberries.ru"
CONTENT = "https://content-api.wildberries.ru"
ACCOUNT = "w"
PAGE_SIZE = 1000
ACTIVE = {"new": "wb_new", "confirm": "wb_confirm", "complete": "wb_complete"}
log = logging.getLogger(__name__)


class Wildberries:
    def __init__(self, client, token, content_token="", write_token=""):
        self.client = client
        self.token = token
        self.content_token = content_token or token
        self.write_token = write_token
        self.next_request = {}
        self.rate_limited_until = {}
        self.titles = {}
        self.content_retry_at = 0
        self.catalog_failure = None

    async def request(self, method, path, *, content=False, label=False, write=False, expected=200, **kwargs):
        origin, token, interval = (CONTENT, self.content_token, 0.75) if content else (MARKETPLACE, self.token, 0.2)
        if write:
            token = self.write_token
            if not token:
                raise MarketplaceError("profile")
        instant = monotonic()
        cooldown = ceil(self.rate_limited_until.get(origin, 0) - instant)
        if cooldown > 0:
            raise MarketplaceError("rate", True, cooldown)
        delay = self.next_request.get(origin, 0) - instant
        if delay > 0:
            await asyncio.sleep(delay)
        self.next_request[origin] = monotonic() + interval
        try:
            reply = await self.client.request(method, origin + path, headers={"Authorization": token}, **kwargs)
        except httpx.RequestError as error:
            raise MarketplaceError("unavailable", True) from error

        code = reply.status_code
        if code == 429:
            after = reply.headers.get("x-ratelimit-retry") or reply.headers.get("retry-after", "")
            wait = min(int(after), 3600) if after.isascii() and after.isdecimal() and len(after) <= 8 else 30
            self.rate_limited_until[origin] = monotonic() + wait
            raise MarketplaceError("rate", True, wait)
        if code >= 500 or code in (408, 425):
            raise MarketplaceError("unavailable", True)
        if code in (401, 402, 403):
            raise MarketplaceError("access")
        if code != expected:
            raise MarketplaceError("label" if label else "response", label and code == 409)
        if expected == 204:
            return None
        try:
            data = reply.json()
        except ValueError as error:
            raise MarketplaceError("response", True) from error
        if not isinstance(data, dict):
            raise MarketplaceError("response", True)
        return data

    async def snapshot(self, key, _known):
        end = datetime.now(UTC)
        start = end - timedelta(days=29)
        params = {"limit": PAGE_SIZE, "next": 0, "dateFrom": int(start.timestamp()), "dateTo": int(end.timestamp())}
        orders = {}
        cursors = {0}
        while True:
            page = await self.request("GET", "/api/v3/orders", params=params)
            rows, cursor = page["orders"], page["next"]
            if not isinstance(rows, list) or type(cursor) is not int or cursor < 0:
                raise MarketplaceError("response", True)
            for row in rows:
                orders[row["id"]] = row
            if not rows or cursor == 0:
                break
            if cursor in cursors:
                raise MarketplaceError("response", True)
            cursors.add(cursor)
            params["next"] = cursor

        statuses = {}
        ids = list(orders)
        for offset in range(0, len(ids), PAGE_SIZE):
            batch = ids[offset:offset + PAGE_SIZE]
            result = await self.request("POST", "/api/v3/orders/status", json={"orders": batch})
            received = {row["id"]: row for row in result["orders"]}
            if received.keys() != set(batch):
                raise MarketplaceError("response", True)
            statuses.update(received)

        active = [
            (row, ACTIVE[status["supplierStatus"]])
            for number, row in orders.items()
            if (status := statuses[number])["wbStatus"] == "waiting" and status["supplierStatus"] in ACTIVE
        ]
        products = {str(row["nmId"]) for row, status in active if status != "wb_complete"}
        await self.update_titles(products)
        missing = {nm for nm in products if not self.titles.get(nm, ("", 0))[0]}
        catalog_status = None
        if missing:
            catalog_status = self.catalog_failure
            if not catalog_status and missing <= self.titles.keys():
                catalog_status = "not_found"
        return {
            "orders": [normalize(key, row, status, self.titles.get(str(row["nmId"]), ("", 0))[0])
                       for row, status in active],
            "covered_since": start.isoformat(),
            "catalog_status": catalog_status,
            "catalog_access_denied": catalog_status == "denied",
        }

    async def update_titles(self, product_ids):
        instant = monotonic()
        if instant < self.content_retry_at:
            return
        missing = sorted(nm for nm in product_ids if self.titles.get(nm, ("", 0))[1] <= instant)
        for nm in missing[:20]:
            try:
                response = await self.request("POST", "/content/v2/get/cards/list", content=True, json={
                    "settings": {"cursor": {"limit": 100}, "filter": {"withPhoto": -1, "textSearch": nm}},
                })
                title = next((card["title"].strip() for card in response["cards"]
                              if str(card.get("nmID")) == nm and isinstance(card.get("title"), str)), "")
            except (MarketplaceError, KeyError, TypeError, ValueError) as error:
                code = getattr(error, "code", "response")
                self.catalog_failure = "denied" if code == "access" else "unavailable"
                delay = 900 if code == "access" else 60
                self.content_retry_at = monotonic() + max(delay, getattr(error, "retry_after", None) or 0)
                log.warning("Не удалось получить названия товаров Wildberries (%s)", code)
                return
            self.catalog_failure = None
            self.titles[nm] = (title, monotonic() + (3600 if title else 600))

    async def check_assembly(self, number, supply_id):
        try:
            supply = await self.request("GET", f"/api/v3/supplies/{supply_id}", write=True)
            if supply.get("id") != supply_id or supply.get("done") is not False:
                raise MarketplaceError("assembly_conflict")
            result = await self.request("POST", "/api/v3/orders/status", write=True, json={"orders": [int(number)]})
        except MarketplaceError as error:
            if error.code == "response" and not error.retry:
                raise MarketplaceError("assembly_conflict") from error
            raise
        order = next((row for row in result["orders"] if row["id"] == int(number)), {})
        if order.get("supplierStatus") != "new" or order.get("wbStatus") != "waiting":
            raise MarketplaceError("assembly_conflict")

    async def move_to_assembly(self, number, supply_id):
        try:
            await self.request("PATCH", f"/api/marketplace/v3/supplies/{supply_id}/orders", write=True,
                               expected=204, timeout=15, json={"orders": [int(number)]})
        except MarketplaceError as error:
            if error.code == "unavailable":
                raise MarketplaceError("assembly_unknown") from error
            if error.code == "response":
                raise MarketplaceError("assembly_conflict") from error
            raise MarketplaceError(error.code) from error

    async def get_sticker(self, number):
        order_id = int(number)
        result = await self.request("POST", "/api/v3/orders/stickers", label=True,
                                    params={"type": "png", "width": 58, "height": 40},
                                    json={"orders": [order_id]})
        sticker = next((row for row in result["stickers"] if row["orderId"] == order_id), None)
        if not sticker:
            raise MarketplaceError("label", True)
        parts = (sticker["partA"], sticker["partB"])
        if not all(isinstance(part, str) and re.fullmatch(r"[A-Za-z0-9-]{1,40}", part) for part in parts):
            raise MarketplaceError("label")
        try:
            data = b64decode(sticker["file"], validate=True)
        except (TypeError, ValueError) as error:
            raise MarketplaceError("label") from error
        if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) > 8 * 1024 * 1024:
            raise MarketplaceError("label")
        return data, " ".join(parts)


def normalize(key, row, status, title):
    price = row.get("convertedPrice")
    currency = row.get("convertedCurrencyCode")
    currency = "₽" if currency == 643 else currency
    return {
        "account_key": key,
        "posting_number": str(row["id"]),
        "order_number": row.get("orderUid"),
        "status": status,
        "created_at": row["createdAt"],
        "warehouse": str(row["warehouseId"]) if row.get("warehouseId") is not None else None,
        "delivery_method": "FBS",
        "supply_id": row.get("supplyId"),
        "products": [{
            "name": title,
            "offer_id": row["article"],
            "sku": str(row["nmId"]),
            "quantity": 1,
            "price": f"{Decimal(price) / 100:.2f} {currency or ''}".rstrip() if type(price) is int else None,
        }],
    }
