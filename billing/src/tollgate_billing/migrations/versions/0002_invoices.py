"""Monthly invoices: invoices, their lines, and which ledger rows each covers (ADR 0017).

Revision ID: 0002
Revises: 0001
Created: 2026-11-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INVOICE = ["tenant_id", "period"]


def upgrade() -> None:
    # An invoice holds the rows recorded in its month.
    op.create_index("ix_usage_ledger_recorded", "usage_ledger", ["recorded_at"])
    op.create_table(
        "invoices",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("period", sa.Date(), primary_key=True),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("total", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "finalized_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("EXTRACT(DAY FROM period) = 1", name="period_is_a_month"),
        sa.CheckConstraint(
            "requests >= 0 AND input_tokens >= 0 AND output_tokens >= 0 AND total >= 0",
            name="invoice_not_negative",
        ),
    )
    op.create_table(
        "invoice_lines",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("period", sa.Date(), primary_key=True),
        sa.Column("line_no", sa.SmallInteger(), primary_key=True),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("meter", sa.String(6), nullable=False),
        sa.Column("band_from", sa.BigInteger(), nullable=False),
        sa.Column("band_up_to", sa.BigInteger(), nullable=True),
        sa.Column("tokens", sa.BigInteger(), nullable=False),
        sa.Column("per_million", sa.Numeric(12, 6), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.ForeignKeyConstraint(INVOICE, ["invoices.tenant_id", "invoices.period"]),
        sa.CheckConstraint("meter IN ('input', 'output')", name="known_meter"),
        sa.CheckConstraint(
            "tokens > 0 AND band_from >= 0 AND (band_up_to IS NULL OR band_up_to > band_from)"
            " AND per_million >= 0 AND amount >= 0",
            name="line_in_range",
        ),
    )
    op.create_table(
        "invoice_items",
        # A ledger row is on one invoice at most: the primary key says so.
        sa.Column(
            "idempotency_key",
            sa.String(100),
            sa.ForeignKey("usage_ledger.idempotency_key"),
            primary_key=True,
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("period", sa.Date(), nullable=False),
        sa.ForeignKeyConstraint(INVOICE, ["invoices.tenant_id", "invoices.period"]),
    )
    op.create_index("ix_invoice_items_invoice", "invoice_items", INVOICE)


def downgrade() -> None:
    op.drop_index("ix_invoice_items_invoice", table_name="invoice_items")
    op.drop_table("invoice_items")
    op.drop_table("invoice_lines")
    op.drop_table("invoices")
    op.drop_index("ix_usage_ledger_recorded", table_name="usage_ledger")
