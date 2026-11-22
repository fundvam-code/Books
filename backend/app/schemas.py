from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


Account = Literal["1", "2", "w"]
AccountFilter = Literal["all", "1", "2", "w"]
Status = Literal["awaiting_packaging", "awaiting_deliver", "wb_new", "wb_confirm", "wb_complete"]


class Product(BaseModel):
    name: str = ""
    offer_id: str = ""
    sku: str = ""
    quantity: int = Field(ge=0)
    price: str | None = None


class Posting(BaseModel):
    account_key: Account
    posting_number: str = Field(pattern=r"^[a-zA-Z0-9-]{1,80}$")
    order_number: str | None = None
    status: Status
    created_at: datetime | None = None
    shipment_date: datetime | None = None
    products: list[Product]
    warehouse: str | None = None
    delivery_method: str | None = None
    supply_id: str | None = None

    @field_validator("created_at", "shipment_date")
    @classmethod
    def to_utc(cls, value):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Не указан часовой пояс")
        return value.astimezone(UTC)


class SyncResult(BaseModel):
    orders: list[Posting]
    covered_since: datetime
    catalog_access_denied: bool = False
    catalog_status: Literal["denied", "unavailable", "not_found"] | None = None


class Completion(BaseModel):
    status: Literal["succeeded", "retryable_error", "permanent_error"]
    result: SyncResult | None = None
    error: Literal["access", "rate", "unavailable", "response", "label", "profile",
                   "assembly_conflict", "assembly_unknown"] | None = None
    retry_after: int | None = Field(default=None, ge=0, le=3600)


class LabelTask(BaseModel):
    ozon_task_id: int = Field(gt=0, le=9223372036854775807)


class PrintResult(BaseModel):
    status: Literal["submitted", "failed", "unknown"]


class AgentAccounts(BaseModel):
    account_keys: list[Account] = Field(min_length=1, max_length=3)
    wb_assembly: bool = False


class AgentHeartbeat(AgentAccounts):
    agent_version: str = Field(max_length=30)
    observed_at: datetime
