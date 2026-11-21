from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


Account = Literal["1", "2"]
Status = Literal["awaiting_packaging", "awaiting_deliver"]


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


class Completion(BaseModel):
    status: Literal["succeeded", "retryable_error", "permanent_error"]
    result: SyncResult | None = None
    error: Literal["access", "rate", "unavailable", "response", "label", "profile"] | None = None
    retry_after: int | None = Field(default=None, ge=0, le=3600)


class LabelTask(BaseModel):
    ozon_task_id: int = Field(gt=0, le=9223372036854775807)


class PrintResult(BaseModel):
    status: Literal["submitted", "failed", "unknown"]


class AgentHeartbeat(BaseModel):
    agent_version: str = Field(max_length=30)
    account_keys: list[Account] = Field(max_length=2)
    observed_at: datetime
