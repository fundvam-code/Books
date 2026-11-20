import os
from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Index, String, Text, URL, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class Cabinet(Base):
    __tablename__ = "cabinets"

    key: Mapped[str] = mapped_column(String(1), primary_key=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class Order(Base):
    __tablename__ = "orders"

    account_key: Mapped[str] = mapped_column(String(1), primary_key=True)
    posting_number: Mapped[str] = mapped_column(String(80), primary_key=True)
    status: Mapped[str] = mapped_column(String(32))
    data: Mapped[dict] = mapped_column(JSONB)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    account_key: Mapped[str] = mapped_column(String(1))
    posting_number: Mapped[str] = mapped_column(String(80), default="")
    state: Mapped[str] = mapped_column(String(16), default="pending")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None]
    ozon_task_id: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(Text)
    label_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "uq_active_job", "kind", "account_key", "posting_number", unique=True,
            postgresql_where=text("state IN ('pending', 'leased')"),
        ),
    )


class Heartbeat(Base):
    __tablename__ = "heartbeat"

    id: Mapped[int] = mapped_column(primary_key=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


engine = create_engine(
    os.environ.get("DATABASE_URL") or URL.create(
        "postgresql+psycopg", username="dashboard", password=os.environ.get("POSTGRES_PASSWORD"),
        host="database", database="dashboard",
    ),
    pool_pre_ping=True,
)
Session = sessionmaker(engine, expire_on_commit=False)
