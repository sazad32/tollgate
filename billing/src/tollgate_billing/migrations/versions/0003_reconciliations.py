"""Nightly reconciliation results: the ledger against the model servers' counters (ADR 0018).

Revision ID: 0003
Revises: 0002
Created: 2026-11-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reconciliations",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("backend", sa.String(64), primary_key=True),
        sa.Column("meter", sa.String(6), primary_key=True),
        sa.Column("ledger_reported", sa.BigInteger(), nullable=False),
        sa.Column("ledger_estimated", sa.BigInteger(), nullable=False),
        sa.Column("server", sa.BigInteger(), nullable=True),
        sa.Column("allowed", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("until", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "checked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("reconciliations")
