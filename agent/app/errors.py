from dataclasses import dataclass


@dataclass
class MarketplaceError(Exception):
    code: str
    retry: bool = False
    retry_after: int | None = None
