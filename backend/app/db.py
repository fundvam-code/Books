import os
from contextlib import contextmanager
from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text, URL, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class Cabinet(Base):
    __tablename__ = "cabinets"

    key: Mapped[str] = mapped_column(String(1), primary_key=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class Shift(Base):
    __tablename__ = "shift"

    id: Mapped[int] = mapped_column(primary_key=True)
    revision: Mapped[UUID]
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Order(Base):
    __tablename__ = "orders"

    account_key: Mapped[str] = mapped_column(String(1), primary_key=True)
    posting_number: Mapped[str] = mapped_column(String(80), primary_key=True)
    status: Mapped[str] = mapped_column(String(32))
    data: Mapped[dict] = mapped_column(JSONB)


class LabelPrint(Base):
    __tablename__ = "label_prints"

    account_key: Mapped[str] = mapped_column(String(1), primary_key=True)
    posting_number: Mapped[str] = mapped_column(String(80), primary_key=True)


class LabelBatch(Base):
    __tablename__ = "label_batches"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    account: Mapped[str] = mapped_column(String(3))
    labels: Mapped[list] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class PrintTask(Base):
    __tablename__ = "print_tasks"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    batch_id: Mapped[UUID] = mapped_column(ForeignKey("label_batches.id", ondelete="CASCADE"))
    fingerprint: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("uq_active_print_task", "fingerprint", unique=True,
              postgresql_where=text("state IN ('pending', 'leased', 'sending')")),
    )


class PrintLog(Base):
    __tablename__ = "print_log"

    task_id: Mapped[UUID] = mapped_column(primary_key=True)
    label_job_id: Mapped[UUID] = mapped_column(primary_key=True)
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    historical: Mapped[bool]
    account_key: Mapped[str] = mapped_column(String(1))
    posting_number: Mapped[str] = mapped_column(String(80))
    book_title: Mapped[str] = mapped_column(Text)
    label_number: Mapped[str | None] = mapped_column(Text)


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


@contextmanager
def write_transaction(*, exclusive=False):
    # Shift reset takes the exclusive form of the lock used by dashboard writes.
    lock = ("SELECT pg_advisory_xact_lock(73608202)" if exclusive
            else "SELECT pg_advisory_xact_lock_shared(73608202)")
    with Session.begin() as db:
        db.execute(text(lock))
        yield db
