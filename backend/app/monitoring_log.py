from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.dialects.postgresql import insert

from .db import MonitoringEvent


def snapshot(order):
    products = order.data.get("products", []) if order else []
    titles = []
    images = []
    for product in products:
        title = " ".join((product.get("name") or "").split())
        if title and title not in titles:
            titles.append(title)
        image = product.get("image_url") or product.get("image_full_url")
        if image and image not in images:
            images.append(image)
    return ", ".join(titles), images


def append_event(
    db, order, event_type, message, *, event_key=None, error=None, label_number=None, event_at=None,
):
    title, images = snapshot(order)
    values = {
        "id": uuid4(),
        "event_at": event_at or datetime.now(UTC),
        "account_key": order.account_key,
        "posting_number": order.posting_number,
        "book_title": title,
        "image_urls": images,
        "label_number": label_number,
        "event_type": event_type,
        "message": message,
        "error": error,
        "event_key": event_key,
    }
    statement = insert(MonitoringEvent).values(**values)
    if event_key:
        statement = statement.on_conflict_do_nothing(index_elements=["event_key"])
        db.execute(statement)
    else:
        db.add(MonitoringEvent(**values))
