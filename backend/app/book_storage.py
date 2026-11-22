import csv
import os
import re
from collections import defaultdict
from io import StringIO
from unicodedata import normalize

import httpx
from sqlalchemy.dialects.postgresql import insert

from .db import BookStorage, write_transaction
from .jobs import now

MAX_CSV_BYTES = 4 * 1024 * 1024
UNKNOWN_PLACES = {"неизвестно", "место не установлено", "нет данных", "-", "—"}
UNVERIFIED_STATUSES = {
    "место не установлено", "требуется подтверждение", "уточнить наличие",
    "частично распознано", "распознано по фото, требуется контроль",
}


def title_key(value):
    return " ".join(re.findall(r"[^\W_]+", normalize("NFKC", value).casefold().replace("ё", "е")))


def isbn_key(value):
    digits = re.sub(r"[\s\-‐‑–]", "", value)
    if not re.fullmatch(r"(?:978|979)[0-9]{10}", digits):
        return ""
    checksum = sum(int(digit) * (1 if index % 2 == 0 else 3) for index, digit in enumerate(digits))
    return digits if checksum % 10 == 0 else ""


def parse_catalog(data):
    reader = csv.DictReader(StringIO(data.decode("utf-8-sig"), newline=""), strict=True)
    if not {"Название", "Место", "Статус", "ISBN"} <= set(reader.fieldnames or ()):
        raise ValueError("Не найдены колонки каталога книг")
    entries = []
    seen = set()
    for row in reader:
        title = title_key(row.get("Название") or "")
        place = " ".join((row.get("Место") or "").split())
        isbn = isbn_key(row.get("ISBN") or "")
        status = " ".join((row.get("Статус") or "").casefold().split())
        entry = (title, isbn, place)
        if ((title or isbn) and place and place.casefold() not in UNKNOWN_PLACES
                and status not in UNVERIFIED_STATUSES and entry not in seen):
            entries.append(entry)
            seen.add(entry)
    return entries


def fetch_catalog(sheet_id, sheet_gid):
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,128}", sheet_id) or not re.fullmatch(r"[0-9]{1,12}", sheet_gid):
        raise ValueError("Некорректный идентификатор таблицы")
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export"
    with httpx.stream("GET", url, params={"format": "csv", "gid": sheet_gid},
                      follow_redirects=True, timeout=15) as response:
        response.raise_for_status()
        data = bytearray()
        for chunk in response.iter_bytes():
            if len(data) + len(chunk) > MAX_CSV_BYTES:
                raise ValueError("Каталог превышает допустимый размер")
            data.extend(chunk)
    return parse_catalog(data)


def refresh_catalog():
    sheet_id = os.environ.get("BOOK_STORAGE_SHEET_ID", "").strip()
    if not sheet_id:
        return "unconfigured"
    started_at = now()
    entries = fetch_catalog(sheet_id, os.environ.get("BOOK_STORAGE_SHEET_GID", "1001"))
    with write_transaction() as db:
        catalog = insert(BookStorage).values(id=1, updated_at=started_at, entries=entries)
        db.execute(catalog.on_conflict_do_update(
            index_elements=["id"],
            set_={"updated_at": catalog.excluded.updated_at, "entries": catalog.excluded.entries},
            where=BookStorage.updated_at <= catalog.excluded.updated_at,
        ))
    return "updated"


class StorageLookup:
    def __init__(self, entries):
        self.by_isbn = defaultdict(list)
        self.by_title = defaultdict(list)
        for title, isbn, place in entries:
            if title:
                self.by_title[title].append((isbn, place))
            if isbn:
                self.by_isbn[isbn].append(place)

    def locations(self, product):
        isbn = isbn_key(product.get("offer_id") or "") or isbn_key(product.get("sku") or "")
        exact = self.by_isbn.get(isbn)
        if exact:
            return tuple(dict.fromkeys(exact))
        matches = self.by_title.get(title_key(product.get("name") or ""), ())
        known_isbns = {candidate for candidate, _ in matches if candidate}
        if known_isbns and ((isbn and isbn not in known_isbns) or len(known_isbns) > 1):
            return ()
        return tuple(dict.fromkeys(place for _, place in matches))
