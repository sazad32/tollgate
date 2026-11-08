"""Writing usage events to the ledger, exactly once (ADR 0016).

The stream delivers at least once: a gateway retry or a consumer crash can hand billing the same
event twice. The ledger makes that harmless: its primary key is the event's idempotency key, and the
insert is `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING`. A copy is skipped, not counted,
however late it comes: the check is the key, not a time window.

One batch is one transaction: the events, the billed tokens and the rejects all commit together, or
none do (and the consumer retries the batch).

.NET: Ledger is an interface; PostgresLedger its implementation over SQLAlchemy Core, closer to
Dapper than to EF change tracking.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from tollgate_billing.db import LedgerRow, RejectRow
from tollgate_billing.events import UsageEvent
from tollgate_billing.policy import billable


@dataclass(frozen=True)
class Entry:
    """An event billing accepted, and the stream entry it came in."""

    stream_id: str
    event: UsageEvent


@dataclass(frozen=True)
class Reject:
    """A stream entry billing couldn't trust: what arrived, and why it was refused."""

    stream_id: str
    fields: dict[str, str]
    reason: str


@dataclass(frozen=True)
class WriteResult:
    inserted: int  # new rows
    duplicates: int  # copies of events already in the ledger, skipped
    rejected: int


class Ledger(Protocol):
    async def write(self, entries: Sequence[Entry], rejects: Sequence[Reject]) -> WriteResult:
        """Write a batch in one transaction. Raises if it couldn't; then nothing was written."""
        ...


def ledger_values(entry: Entry) -> dict:
    event = entry.event
    billed_in, billed_out = billable(event)
    return {
        "idempotency_key": event.idempotency_key,
        "request_id": event.request_id,
        "tenant_id": event.tenant_id,
        "credential": event.credential,
        "model": event.model,
        "backend": event.backend,
        "input_tokens": event.input_tokens,
        "output_tokens": event.output_tokens,
        "token_source": event.token_source,
        "status": event.status,
        "stream": event.stream,
        "billable_input_tokens": billed_in,
        "billable_output_tokens": billed_out,
        "started_at": event.started_at,
        "finished_at": event.finished_at,
        "schema_version": event.version,
        "stream_id": entry.stream_id,
    }


class PostgresLedger:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def write(self, entries: Sequence[Entry], rejects: Sequence[Reject]) -> WriteResult:
        # Two copies in one batch: keep the first. (Postgres would skip the second too; this keeps
        # the statement simple to reason about.)
        rows: dict[str, dict] = {}
        for entry in entries:
            rows.setdefault(entry.event.idempotency_key, ledger_values(entry))

        inserted = 0
        async with (
            self.engine.begin() as conn
        ):  # one transaction: commits on exit, rolls back on error
            if rows:
                result = await conn.execute(
                    insert(LedgerRow)
                    .values(list(rows.values()))
                    .on_conflict_do_nothing(index_elements=["idempotency_key"])
                    .returning(LedgerRow.idempotency_key)
                )
                inserted = len(result.all())  # RETURNING lists only the rows really inserted
            if rejects:
                # Keyed by stream entry: replaying the stream doesn't reject the same entry twice.
                await conn.execute(
                    insert(RejectRow)
                    .values(
                        [
                            {
                                "stream_id": r.stream_id,
                                "fields": json.dumps(r.fields, sort_keys=True),
                                "reason": r.reason,
                            }
                            for r in rejects
                        ]
                    )
                    .on_conflict_do_nothing(index_elements=["stream_id"])
                )
        return WriteResult(inserted, len(entries) - inserted, len(rejects))
