import re
from dataclasses import dataclass


def response_detail(response):
    prefix = f"HTTP {response.status_code}"
    if not response.content:
        return f"{prefix}: пустое тело ответа"
    try:
        data = response.json()
    except ValueError:
        return f"{prefix}: ответ не является JSON"
    if not isinstance(data, dict):
        return f"{prefix}: ожидался JSON-объект"
    for key in ("code", "errorCode", "error_code"):
        code = data.get(key)
        if type(code) is int or (
            isinstance(code, str) and re.fullmatch(r"[A-Z]+(?:_[A-Z0-9]+)+|[0-9]{1,9}", code)
            and len(code) <= 60
        ):
            return f"{prefix}: код ошибки API {code}"
    return f"{prefix}: JSON без распознанного кода ошибки"


@dataclass
class MarketplaceError(Exception):
    code: str
    retry: bool = False
    retry_after: int | None = None
    detail: str | None = None
