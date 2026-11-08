"""The ledger against a real Postgres: ON CONFLICT is the exactly-once guarantee, so it's tested on
the real thing, not a stand-in.

Needs TOLLGATE_TEST_DATABASE_URL (CI sets it; locally, a throwaway container: billing/README.md).
The schema is built from scratch by the migration, run down then up, so both directions are tested.
"""

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError
from tollgate_billing.db import LedgerRow, RejectRow, make_engine
from tollgate_billing.events import parse
from tollgate_billing.ledger import Entry, PostgresLedger, Reject
from tollgate_billing.migrate import downgrade, upgrade

URL = os.environ.get("TOLLGATE_TEST_DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(not URL, reason="set TOLLGATE_TEST_DATABASE_URL to run the Postgres tests"),
    pytest.mark.asyncio,
]


def in_thread(fn, *args) -> None:
    # Alembic's env.py starts its own event loop: run it away from pytest-asyncio's.
    with ThreadPoolExecutor(1) as pool:
        pool.submit(fn, *args).result()


@pytest.fixture(scope="module")
def schema() -> None:
    in_thread(downgrade, URL)
    in_thread(upgrade, URL)


@pytest_asyncio.fixture
async def engine(schema):
    engine = make_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE usage_ledger, usage_rejects"))
    yield engine
    await engine.dispose()


@pytest.fixture
def entry(event_fields):
    def make(stream_id: str = "1731080000000-0", **overrides: str) -> Entry:
        return Entry(stream_id, parse(event_fields(**overrides)))

    return make


async def ledger_rows(engine) -> list[LedgerRow]:
    async with engine.connect() as conn:
        return list((await conn.execute(select(LedgerRow.__table__))).all())


async def test_an_event_becomes_a_ledger_row(engine, entry) -> None:
    result = await PostgresLedger(engine).write([entry()], [])

    assert (result.inserted, result.duplicates, result.rejected) == (1, 0, 0)
    [row] = await ledger_rows(engine)
    assert row.idempotency_key == "9f2c4b7e1d3a5c6b8e0f1a2b3c4d5e6f:usage"
    assert (row.tenant_id, row.model, row.backend) == ("support", "smollm3-3b", "llama-server-0")
    assert (row.input_tokens, row.output_tokens) == (74, 60)
    assert (row.billable_input_tokens, row.billable_output_tokens) == (74, 60)
    assert (row.token_source, row.status, row.stream) == ("reported", "completed", True)
    assert row.started_at == datetime(2026, 11, 8, 17, 22, 4, 519000, tzinfo=UTC)
    assert (row.schema_version, row.stream_id) == (1, "1731080000000-0")
    assert row.recorded_at is not None


async def test_a_copy_of_an_event_is_skipped_however_late(engine, entry) -> None:
    ledger = PostgresLedger(engine)
    await ledger.write([entry("1731080000000-0")], [])

    # The same event again, in a later stream entry (a gateway retry, or a replay).
    result = await ledger.write([entry("1731080999999-0")], [])

    assert (result.inserted, result.duplicates) == (0, 1)
    [row] = await ledger_rows(engine)
    assert row.stream_id == "1731080000000-0"  # the first copy stays; nothing is overwritten


async def test_two_copies_in_one_batch_count_once(engine, entry) -> None:
    result = await PostgresLedger(engine).write([entry("1-0"), entry("2-0")], [])
    assert (result.inserted, result.duplicates) == (1, 1)
    assert len(await ledger_rows(engine)) == 1


async def test_two_writers_racing_with_the_same_event_count_it_once(engine, entry) -> None:
    # Two consumers (or a consumer and its replacement) with the same event at the same moment:
    # Postgres's unique index makes one wait for the other, then skip.
    ledger = PostgresLedger(engine)
    results = await asyncio.gather(*(ledger.write([entry(f"{n}-0")], []) for n in range(5)))
    assert sum(r.inserted for r in results) == 1
    assert len(await ledger_rows(engine)) == 1


async def test_a_failed_request_is_recorded_but_not_billed(engine, entry) -> None:
    await PostgresLedger(engine).write([entry(status="failed", output_tokens="12")], [])
    [row] = await ledger_rows(engine)
    assert (row.input_tokens, row.output_tokens) == (74, 12)
    assert (row.billable_input_tokens, row.billable_output_tokens) == (0, 0)


async def test_rejects_are_kept_once_with_what_arrived(engine) -> None:
    ledger = PostgresLedger(engine)
    reject = Reject("1731080000001-0", {"v": "2", "model": "\\xffsmol"}, "unsupported version")
    await ledger.write([], [reject])
    await ledger.write([], [reject])  # replayed: still one row

    async with engine.connect() as conn:
        rows = (await conn.execute(select(RejectRow.__table__))).all()
    assert len(rows) == 1
    assert json.loads(rows[0].fields) == {"v": "2", "model": "\\xffsmol"}
    assert rows[0].reason == "unsupported version"


async def test_a_batch_commits_whole_or_not_at_all(engine, entry) -> None:
    # The reject's stream ID is too long for its column, so its insert fails after the event's
    # insert ran. The transaction rolls both back: the consumer will retry the batch whole.
    bad = Reject("9" * 50, {}, "x")
    with pytest.raises(DBAPIError):
        await PostgresLedger(engine).write([entry()], [bad])
    assert await ledger_rows(engine) == []


async def test_the_database_refuses_billing_more_than_was_measured(engine) -> None:
    # The check constraint behind policy.py: even a bug can't bill more than was used.
    async with engine.connect() as conn:
        with pytest.raises(DBAPIError, match="billed_within_measured"):
            await conn.execute(
                text(
                    "INSERT INTO usage_ledger VALUES ('k', 'r', 't', 'c', 'm', 'b', 1, 1, "
                    "'reported', 'completed', false, 2, 1, now(), now(), 1, '1-0', now())"
                )
            )


async def test_the_migration_runs_both_ways(engine) -> None:
    async def tables() -> set[str]:
        async with engine.connect() as conn:
            return set(await conn.run_sync(lambda c: inspect(c).get_table_names()))

    await asyncio.to_thread(downgrade, URL)
    assert not {"usage_ledger", "usage_rejects"} & await tables()
    await asyncio.to_thread(upgrade, URL)
    assert {"usage_ledger", "usage_rejects"} <= await tables()


# --- end to end: the consumer with the real ledger -----------------------------------------------


async def test_the_consumer_bills_each_event_once_in_postgres(engine, redis, event_fields) -> None:
    from tollgate_billing.__main__ import status_lines
    from tollgate_billing.consumer import Consumer

    ids = [f"{n:032x}" for n in range(1, 4)]
    for request_id in ids:
        await redis.xadd("tollgate:usage", event_fields(request_id=request_id))
    await redis.xadd("tollgate:usage", event_fields(request_id=ids[0]))  # a gateway retry's copy
    await redis.xadd("tollgate:usage", event_fields(v="9"))  # one billing can't read
    await redis.xadd(
        "tollgate:usage",
        event_fields(request_id=f"{9:032x}", tenant_id="engineering", status="failed"),
    )

    consumer = (
        Consumer(PostgresLedger(engine), name="billing-test", block_ms=10)
        if False
        else Consumer(redis, PostgresLedger(engine), name="billing-test", block_ms=10)
    )
    assert await consumer.step() == 6
    assert (consumer.inserted, consumer.duplicates, consumer.rejected) == (4, 1, 1)

    lines = await status_lines(redis, engine, "tollgate:usage", "billing")
    assert lines[0].startswith("stream tollgate:usage: 6 entries")
    assert lines[1] == "group billing: 0 pending, 0 not yet read"
    assert any("ledger: 4 rows" in line and "rejects: 1" in line for line in lines)
    tenants = {line.split()[0]: line.split()[1:] for line in lines if line.startswith("  ")
               and line.split()[0] in ("support", "engineering")}  # fmt: skip
    assert tenants["support"] == ["3", "222", "180", "222", "180"]
    assert tenants["engineering"] == ["1", "0", "0", "74", "60"]  # failed: measured, not billed
