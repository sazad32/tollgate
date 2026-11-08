"""Billing's tables, in its own `billing` database (ADR 0011: one database and login per app).

- usage_ledger: one row per request a model ran, keyed by the event's idempotency key. Append-only:
  rows are never updated or deleted; a correction (week 13) is a new row.
- usage_rejects: stream entries billing couldn't trust, with the reason, keyed by stream entry ID.

.NET: SQLAlchemy declarative classes are EF Core entities; Alembic (migrations/) is EF migrations.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class LedgerRow(Base):
    __tablename__ = "usage_ledger"
    __table_args__ = (
        # Belt and braces: the event reader already refuses all of these (events.py).
        CheckConstraint("input_tokens >= 0 AND output_tokens >= 0", name="tokens_not_negative"),
        CheckConstraint(
            "billable_input_tokens BETWEEN 0 AND input_tokens"
            " AND billable_output_tokens BETWEEN 0 AND output_tokens",
            name="billed_within_measured",
        ),
        CheckConstraint("token_source IN ('reported', 'estimated')", name="known_token_source"),
        CheckConstraint(
            "status IN ('completed', 'client_disconnected', 'failed')", name="known_status"
        ),
        # Invoices (week 12): one tenant's rows in a month.
        Index("ix_usage_ledger_tenant_started", "tenant_id", "started_at"),
    )

    # The exactly-once guarantee: a second copy of an event hits this key and is skipped.
    idempotency_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(32))
    tenant_id: Mapped[str] = mapped_column(String(64))
    credential: Mapped[str] = mapped_column(String(200))
    model: Mapped[str] = mapped_column(String(200))
    backend: Mapped[str] = mapped_column(String(64))
    # What the request measured: the cost to us.
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    token_source: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32))
    stream: Mapped[bool] = mapped_column(Boolean)
    # What the tenant pays for (policy.py).
    billable_input_tokens: Mapped[int] = mapped_column(Integer)
    billable_output_tokens: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    schema_version: Mapped[int] = mapped_column(SmallInteger)
    # Where it came from: the Redis stream entry that was written first.
    stream_id: Mapped[str] = mapped_column(String(41))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RejectRow(Base):
    __tablename__ = "usage_rejects"

    stream_id: Mapped[str] = mapped_column(String(41), primary_key=True)
    fields: Mapped[str] = mapped_column(Text)  # the entry as JSON text, exactly as it arrived
    reason: Mapped[str] = mapped_column(Text)
    rejected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


def make_engine(database_url: str) -> AsyncEngine:
    # pool_pre_ping: a Postgres restart costs one retry, not a failed batch.
    return create_async_engine(database_url, pool_size=2, max_overflow=2, pool_pre_ping=True)
