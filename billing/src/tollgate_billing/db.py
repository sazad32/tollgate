"""Billing's tables, in its own `billing` database (ADR 0011: one database and login per app).

- usage_ledger: one row per request a model ran, keyed by the event's idempotency key. Append-only:
  rows are never updated or deleted; a correction (week 13) is a new row.
- usage_rejects: stream entries billing couldn't trust, with the reason, keyed by stream entry ID.
- invoices, invoice_lines: one finalized invoice per tenant per month, and its priced lines. Never
  changed once written (ADR 0017).
- invoice_items: which invoice each ledger row is on. Keyed by the ledger row, so Postgres itself
  refuses to put a row on two invoices.
- reconciliations: each night's comparison of the ledger with the model servers' own token counters
  (ADR 0018). A measurement, not money: running a day again replaces its result.

.NET: SQLAlchemy declarative classes are EF Core entities; Alembic (migrations/) is EF migrations.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
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
        # A tenant's usage over time (the dashboard, Phase 5).
        Index("ix_usage_ledger_tenant_started", "tenant_id", "started_at"),
        # Invoices: the rows recorded in a month (ADR 0017).
        Index("ix_usage_ledger_recorded", "recorded_at"),
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
    # When billing wrote the row: the month it is invoiced in (ADR 0017). Set by Postgres to the
    # start of the writing transaction, never by the event, so no row lands in a closed month.
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


class InvoiceRow(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        CheckConstraint("EXTRACT(DAY FROM period) = 1", name="period_is_a_month"),
        CheckConstraint(
            "requests >= 0 AND input_tokens >= 0 AND output_tokens >= 0 AND total >= 0",
            name="invoice_not_negative",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    period: Mapped[date] = mapped_column(Date, primary_key=True)  # the month's first day
    currency: Mapped[str] = mapped_column(String(3))
    # Totals of the billed tokens on the ledger rows it covers (invoice_items).
    requests: Mapped[int] = mapped_column(Integer)
    input_tokens: Mapped[int] = mapped_column(BigInteger)
    output_tokens: Mapped[int] = mapped_column(BigInteger)
    total: Mapped[Decimal] = mapped_column(Numeric(14, 2))  # the sum of its lines
    finalized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class InvoiceLineRow(Base):
    __tablename__ = "invoice_lines"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "period"], ["invoices.tenant_id", "invoices.period"]),
        CheckConstraint("meter IN ('input', 'output')", name="known_meter"),
        CheckConstraint(
            "tokens > 0 AND band_from >= 0 AND (band_up_to IS NULL OR band_up_to > band_from)"
            " AND per_million >= 0 AND amount >= 0",
            name="line_in_range",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    period: Mapped[date] = mapped_column(Date, primary_key=True)
    line_no: Mapped[int] = mapped_column(SmallInteger, primary_key=True)  # from 1
    model: Mapped[str] = mapped_column(String(200))
    meter: Mapped[str] = mapped_column(String(6))
    # The price band, copied from the price list so the invoice reads the same after prices change.
    band_from: Mapped[int] = mapped_column(BigInteger)
    band_up_to: Mapped[int | None] = mapped_column(BigInteger)
    tokens: Mapped[int] = mapped_column(BigInteger)
    per_million: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))


class InvoiceItemRow(Base):
    __tablename__ = "invoice_items"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "period"], ["invoices.tenant_id", "invoices.period"]),
        Index("ix_invoice_items_invoice", "tenant_id", "period"),
    )

    # The primary key is the ledger row: on one invoice at most, whatever runs twice.
    idempotency_key: Mapped[str] = mapped_column(
        String(100), ForeignKey("usage_ledger.idempotency_key"), primary_key=True
    )
    tenant_id: Mapped[str] = mapped_column(String(64))
    period: Mapped[date] = mapped_column(Date)


class ReconciliationRow(Base):
    __tablename__ = "reconciliations"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    backend: Mapped[str] = mapped_column(String(64), primary_key=True)
    meter: Mapped[str] = mapped_column(String(6), primary_key=True)  # "output" (ADR 0018)
    ledger_reported: Mapped[int] = mapped_column(BigInteger)
    ledger_estimated: Mapped[int] = mapped_column(BigInteger)
    server: Mapped[int | None] = mapped_column(BigInteger)  # None: no samples that day
    allowed: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16))
    # The span compared: from the counter's sample before the day to its last sample in it.
    since: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def make_engine(database_url: str) -> AsyncEngine:
    # pool_pre_ping: a Postgres restart costs one retry, not a failed batch.
    return create_async_engine(database_url, pool_size=2, max_overflow=2, pool_pre_ping=True)
