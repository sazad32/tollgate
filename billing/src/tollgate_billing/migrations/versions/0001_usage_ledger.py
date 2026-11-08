"""The usage ledger and the rejects table (ADR 0016).

Revision ID: 0001
Revises:
Created: 2026-11-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "usage_ledger",
        # The idempotency key: a second copy of an event conflicts here and is skipped.
        sa.Column("idempotency_key", sa.String(100), primary_key=True),
        sa.Column("request_id", sa.String(32), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("credential", sa.String(200), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("backend", sa.String(64), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("token_source", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("stream", sa.Boolean(), nullable=False),
        sa.Column("billable_input_tokens", sa.Integer(), nullable=False),
        sa.Column("billable_output_tokens", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_version", sa.SmallInteger(), nullable=False),
        sa.Column("stream_id", sa.String(41), nullable=False),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("input_tokens >= 0 AND output_tokens >= 0", name="tokens_not_negative"),
        sa.CheckConstraint(
            "billable_input_tokens BETWEEN 0 AND input_tokens"
            " AND billable_output_tokens BETWEEN 0 AND output_tokens",
            name="billed_within_measured",
        ),
        sa.CheckConstraint("token_source IN ('reported', 'estimated')", name="known_token_source"),
        sa.CheckConstraint(
            "status IN ('completed', 'client_disconnected', 'failed')", name="known_status"
        ),
    )
    op.create_index("ix_usage_ledger_tenant_started", "usage_ledger", ["tenant_id", "started_at"])
    op.create_table(
        "usage_rejects",
        sa.Column("stream_id", sa.String(41), primary_key=True),
        sa.Column("fields", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "rejected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("usage_rejects")
    op.drop_index("ix_usage_ledger_tenant_started", table_name="usage_ledger")
    op.drop_table("usage_ledger")
