"""Shared test setup for billing: usage-event fields as the gateway writes them, fake Redis, an
in-memory ledger, and (when TOLLGATE_TEST_DATABASE_URL is set) a real Postgres.

.NET: fixtures are like xUnit class/collection fixtures, injected by parameter name.
"""

from collections.abc import Callable, Sequence

import fakeredis
import pytest
import pytest_asyncio
from tollgate_billing.ledger import Entry, Reject, WriteResult

REQUEST_ID = "9f2c4b7e1d3a5c6b8e0f1a2b3c4d5e6f"


def base_fields(**overrides: str) -> dict[str, str]:
    fields = {
        "v": "1",
        "idempotency_key": f"{REQUEST_ID}:usage",
        "request_id": REQUEST_ID,
        "tenant_id": "support",
        "credential": "key:4d1a",
        "model": "smollm3-3b",
        "backend": "llama-server-0",
        "input_tokens": "74",
        "output_tokens": "60",
        "token_source": "reported",
        "status": "completed",
        "stream": "true",
        "started_at": "2026-11-08T17:22:04.519Z",
        "finished_at": "2026-11-08T17:22:06.002Z",
    }
    if "request_id" in overrides and "idempotency_key" not in overrides:
        overrides["idempotency_key"] = f"{overrides['request_id']}:usage"
    fields.update(overrides)
    return fields


@pytest.fixture
def event_fields() -> Callable[..., dict[str, str]]:
    """Build one usage event's stream fields; keyword arguments override single fields."""
    return base_fields


class MemoryLedger:
    """The ledger's contract, kept in a dict: a key is written once, later copies are duplicates.
    `fail` makes the next writes raise, like Postgres being down."""

    def __init__(self) -> None:
        self.rows: dict[str, Entry] = {}
        self.rejects: dict[str, Reject] = {}
        self.fail: Exception | None = None
        self.writes = 0

    async def write(self, entries: Sequence[Entry], rejects: Sequence[Reject]) -> WriteResult:
        if self.fail is not None:
            raise self.fail
        self.writes += 1
        inserted = 0
        for entry in entries:
            if entry.event.idempotency_key not in self.rows:
                self.rows[entry.event.idempotency_key] = entry
                inserted += 1
        for reject in rejects:
            self.rejects.setdefault(reject.stream_id, reject)
        return WriteResult(inserted=inserted, duplicates=len(entries) - inserted,
                           rejected=len(rejects))  # fmt: skip


@pytest.fixture
def ledger() -> MemoryLedger:
    return MemoryLedger()


@pytest_asyncio.fixture
async def redis():
    client = fakeredis.FakeAsyncRedis()
    yield client
    await client.aclose()
