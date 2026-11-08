"""The request audit (ADR 0018): each request as its client saw it, against its ledger row.

The client records come from `tollgate_bench metering`, one JSON line per request, keyed by the
gateway's request ID (the X-Request-ID response header). Used for the Phase 3 gate: every request
a client completed must be in the ledger with exactly the usage the client was told.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from tollgate_billing.db import LedgerRow


@dataclass(frozen=True)
class ClientRecord:
    request_id: str | None
    tenant: str
    outcome: str  # completed | hung_up | error
    input_tokens: int | None  # the usage the client was told; None if it never got it
    output_tokens: int | None
    chunks: int  # content chunks the client received (streams)


@dataclass(frozen=True)
class LedgerView:
    request_id: str
    tenant_id: str
    status: str
    token_source: str
    input_tokens: int
    output_tokens: int
    billable_input_tokens: int
    billable_output_tokens: int


@dataclass
class AuditResult:
    matched: int = 0  # completed, and the row is exactly what the client saw
    hung_up: int = 0  # the client hung up, and the row bills it
    refused: int = 0  # the client got an error, and nothing is billed
    unchecked: int = 0  # no request ID to look up
    problems: list[str] = field(default_factory=list)


def read_records(lines: Iterable[str]) -> list[ClientRecord]:
    records = []
    for line in lines:
        if line.strip():
            d = json.loads(line)
            records.append(
                ClientRecord(
                    d["request_id"],
                    d["tenant"],
                    d["outcome"],
                    d["input_tokens"],
                    d["output_tokens"],
                    d["chunks"],
                )
            )
    return records


def audit(records: list[ClientRecord], rows: dict[str, LedgerView]) -> AuditResult:
    result = AuditResult()
    for r in records:
        if r.request_id is None:
            result.unchecked += 1
            continue
        row = rows.get(r.request_id)
        if r.outcome == "error":
            if row is not None and (row.billable_input_tokens or row.billable_output_tokens):
                result.problems.append(
                    f"{r.request_id}: the client got an error but the ledger bills "
                    f"{row.billable_input_tokens} in, {row.billable_output_tokens} out"
                )
            else:  # refused before a model ran, or failed and billed at nothing
                result.refused += 1
            continue
        if row is None:
            result.problems.append(f"{r.request_id}: {r.outcome.replace('_', ' ')} but not in "
                                   f"the ledger")  # fmt: skip
            continue
        if row.tenant_id != r.tenant:
            result.problems.append(f"{r.request_id}: sent by {r.tenant} but billed to "
                                   f"{row.tenant_id}")  # fmt: skip
            continue
        if r.outcome == "completed":
            seen = (r.input_tokens, r.output_tokens)
            if seen != (row.input_tokens, row.output_tokens):
                result.problems.append(
                    f"{r.request_id}: the client saw {seen[0]} in, {seen[1]} out; the ledger has "
                    f"{row.input_tokens} in, {row.output_tokens} out"
                )
            elif (row.billable_input_tokens, row.billable_output_tokens) != seen:
                result.problems.append(
                    f"{r.request_id}: completed but billed {row.billable_input_tokens} in, "
                    f"{row.billable_output_tokens} out of {seen[0]} in, {seen[1]} out"
                )
            else:
                result.matched += 1
        else:  # hung_up: billed the estimate, or in full if it finished first
            if row.output_tokens < r.chunks:
                result.problems.append(f"{r.request_id}: the client received {r.chunks} chunks; "
                                       f"the ledger has {row.output_tokens} out")  # fmt: skip
            elif (row.billable_input_tokens, row.billable_output_tokens) != (
                row.input_tokens, row.output_tokens,
            ):  # fmt: skip
                result.problems.append(f"{r.request_id}: hung up but not billed what it measured")
            else:
                result.hung_up += 1
    return result


async def ledger_rows(engine: AsyncEngine, request_ids: list[str]) -> dict[str, LedgerView]:
    if not request_ids:
        return {}
    query = select(
        LedgerRow.request_id, LedgerRow.tenant_id, LedgerRow.status, LedgerRow.token_source,
        LedgerRow.input_tokens, LedgerRow.output_tokens, LedgerRow.billable_input_tokens,
        LedgerRow.billable_output_tokens,
    ).where(LedgerRow.request_id.in_(request_ids))  # fmt: skip
    async with engine.connect() as conn:
        return {r.request_id: LedgerView(*r) for r in (await conn.execute(query)).all()}
