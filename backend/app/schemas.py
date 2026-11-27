from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


Account = Literal["1", "2", "w"]
AccountFilter = Literal["all", "oz", "1", "2", "w"]
Status = Literal["awaiting_packaging", "awaiting_deliver", "wb_new", "wb_confirm", "wb_complete"]


class Product(BaseModel):
    name: str = ""
    offer_id: str = ""
    sku: str = ""
    quantity: int = Field(ge=0)
    price: str | None = None
    image_url: str | None = None
    image_full_url: str | None = None
    listing_created_at: str | None = None


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
    product_info_status: Literal["denied", "unavailable"] | None = None


class Completion(BaseModel):
    status: Literal["succeeded", "retryable_error", "permanent_error"]
    result: SyncResult | None = None
    error: Literal["access", "rate", "unavailable", "response", "label", "profile",
                   "assembly_conflict", "assembly_unknown", "assembly_no_supply", "assembly_multiple_supplies",
                    "ozon_assembly_conflict", "ozon_assembly_unknown"] | None = None
    detail: str | None = Field(default=None, max_length=1200)
    retry_after: int | None = Field(default=None, ge=0, le=3600)
    assembly_status: Literal["awaiting_deliver"] | None = None


class LabelTask(BaseModel):
    ozon_task_id: int = Field(gt=0, le=9223372036854775807)


class AssemblyStart(BaseModel):
    supply_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")


class PrintResult(BaseModel):
    status: Literal["submitted", "failed", "unknown"]


class MonitoringSettings(BaseModel):
    enabled: bool
    ozon_enabled: bool
    wb_enabled: bool


class AgentAccounts(BaseModel):
    account_keys: list[Account] = Field(min_length=1, max_length=3)
    ozon_assembly_accounts: list[Literal["1", "2"]] = Field(default_factory=list)
    ozon_print_accounts: list[Literal["1", "2"]] = Field(default_factory=list)
    wb_assembly: bool = False
    wb_auto_assembly: bool = False
    print_enabled: bool = False


class AgentHeartbeat(AgentAccounts):
    agent_version: str = Field(max_length=30)
    observed_at: datetime
