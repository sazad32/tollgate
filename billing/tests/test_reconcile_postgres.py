"""Reconciliation and the request audit against a real Postgres (ADR 0018), with a fake Prometheus.

Needs TOLLGATE_TEST_DATABASE_URL, like test_ledger_postgres.py.
"""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import insert, inspect, select, text
from tollgate_billing.audit import LedgerView, ledger_rows
from tollgate_billing.db import LedgerRow, ReconciliationRow, make_engine
from tollgate_billing.migrate import downgrade, upgrade
from tollgate_billing.reconcile import ReconcileBackend, Tolerance, reconcile, store

URL = os.environ.get("TOLLGATE_TEST_DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(not URL, reason="set TOLLGATE_TEST_DATABASE_URL to run the Postgres tests"),
    pytest.mark.asyncio,
]

DAY = date(2026, 11, 7)
START = datetime(2026, 11, 7, tzinfo=UTC)
END = START + timedelta(days=1)
BACKENDS = [
    ReconcileBackend(backend="llama-server-0", kind="llama"),
    ReconcileBackend(backend="llama-server-1", kind="llama"),
]


def in_thread(fn, *args) -> None:
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
        await conn.execute(text("TRUNCATE usage_ledger, usage_rejects, invoices, reconciliations "
                                "CASCADE"))  # fmt: skip
    yield engine
    await engine.dispose()


def ledger_row(n: int, finished_at: datetime, *, backend: str = "llama-server-0",
               output: int = 40, source: str = "reported",
               status: str = "completed") -> dict:  # fmt: skip
    return {
        "idempotency_key": f"{n:032x}:usage", "request_id": f"{n:032x}", "tenant_id": "support",
        "credential": "key:test", "model": "smollm3-3b", "backend": backend,
        "input_tokens": 75, "output_tokens": output, "token_source": source, "status": status,
        "stream": True, "billable_input_tokens": 75, "billable_output_tokens": output,
        "started_at": finished_at - timedelta(seconds=3), "finished_at": finished_at,
        "schema_version": 1, "stream_id": f"{n}-0", "recorded_at": finished_at,
    }  # fmt: skip


async def add(engine, *rows: dict) -> None:
    async with engine.begin() as conn:
        await conn.execute(insert(LedgerRow), list(rows))


def prometheus(samples: dict[str, list[list[tuple[float, float]]]]) -> httpx.MockTransport:
    """A fake Prometheus: raw samples per `backend` label."""

    def handle(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        label = query.split('backend="')[1].split('"')[0]
        result = [{"metric": {"backend": label}, "values": [[t, str(v)] for t, v in series]}
                  for series in samples.get(label, [])]  # fmt: skip
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "matrix", "result": result}}
        )

    return httpx.MockTransport(handle)


def at(hours: float) -> datetime:
    return START + timedelta(hours=hours)


async def test_the_ledger_is_compared_over_exactly_the_span_the_counter_covers(engine) -> None:
    await add(
        engine,
        ledger_row(1, at(1)),
        ledger_row(2, at(2)),
        ledger_row(3, at(3), output=19, source="estimated", status="client_disconnected"),
        ledger_row(4, START - timedelta(seconds=30)),  # before the counter's first sample
        ledger_row(5, at(23.99)),  # after its last one: the next day's check counts it
        ledger_row(6, at(4), backend="llama-server-1"),
    )
    first, last = START - timedelta(seconds=10), at(23.9)
    # Generated: 40 + 40 + 21 (the hang-up ran 2 tokens past what the gateway relayed)
    samples = {"llama-server-0": [[(first.timestamp(), 1000.0), (at(12).timestamp(), 1080.0),
                                   (last.timestamp(), 1101.0)]]}  # fmt: skip

    async with httpx.AsyncClient(transport=prometheus(samples)) as http:
        checks = await reconcile(engine, http, "http://prom", BACKENDS, START, END, Tolerance())

    zero, one = checks
    assert (zero.backend, zero.reported, zero.estimated, zero.server) == ("llama-server-0", 80, 19,
                                                                          101)  # fmt: skip
    assert (zero.gap, zero.allowed, zero.status) == (2, 9, "within tolerance")
    assert (zero.since, zero.until) == (first, last)
    assert (one.backend, one.server, one.ledger, one.status) == ("llama-server-1", None, 40,
                                                                 "no data")  # fmt: skip


async def test_a_lost_event_shows_as_a_gap(engine) -> None:
    await add(engine, ledger_row(1, at(1)))
    samples = {"llama-server-0": [[(START.timestamp() - 10, 0.0), (at(2).timestamp(), 80.0)]]}

    async with httpx.AsyncClient(transport=prometheus(samples)) as http:
        [check, _] = await reconcile(engine, http, "http://prom", BACKENDS, START, END, Tolerance())

    assert (check.gap, check.status, check.failed) == (40, "gap", True)


async def test_a_days_result_is_stored_once_and_a_rerun_replaces_it(engine) -> None:
    samples = {"llama-server-0": [[(START.timestamp() - 10, 0.0), (at(2).timestamp(), 40.0)]]}
    await add(engine, ledger_row(1, at(1)))
    async with httpx.AsyncClient(transport=prometheus(samples)) as http:
        await store(engine, DAY, await reconcile(engine, http, "http://prom", BACKENDS, START, END,
                                                 Tolerance()))  # fmt: skip
        await add(engine, ledger_row(2, at(1.5)))
        await store(engine, DAY, await reconcile(engine, http, "http://prom", BACKENDS, START, END,
                                                 Tolerance()))  # fmt: skip

    async with engine.connect() as conn:
        rows = (await conn.execute(select(ReconciliationRow.__table__)
                                   .order_by(ReconciliationRow.backend))).all()  # fmt: skip
    assert [(r.day, r.backend, r.meter, r.ledger_reported, r.server, r.status) for r in rows] == [
        (DAY, "llama-server-0", "output", 80, 40, "gap"),
        (DAY, "llama-server-1", "output", 0, None, "idle"),
    ]


async def test_the_audit_reads_the_ledger_rows_it_is_asked_for(engine) -> None:
    await add(engine, ledger_row(1, at(1)), ledger_row(2, at(2)))

    rows = await ledger_rows(engine, [f"{1:032x}", f"{9:032x}"])

    assert rows == {f"{1:032x}": LedgerView(f"{1:032x}", "support", "completed", "reported",
                                             75, 40, 75, 40)}  # fmt: skip
    assert await ledger_rows(engine, []) == {}


async def test_the_reconciliation_migration_runs_both_ways(engine) -> None:
    async def tables() -> set[str]:
        async with engine.connect() as conn:
            return set(await conn.run_sync(lambda c: inspect(c).get_table_names()))

    await asyncio.to_thread(downgrade, URL, "0002")
    assert "reconciliations" not in await tables()
    assert "invoices" in await tables()
    await asyncio.to_thread(upgrade, URL)
    assert "reconciliations" in await tables()


# --- the commands ---------------------------------------------------------------------------------


def settings():
    from tollgate_billing.config import Settings

    return Settings(database_url=URL, prometheus_url="http://prom", reconcile_backends=BACKENDS)


async def run_reconcile(*argv: str, now: datetime, transport) -> int:
    from tollgate_billing.__main__ import parser, reconcile_command

    return await reconcile_command(settings(), parser().parse_args(["reconcile", *argv]), now,
                                   transport)  # fmt: skip


async def test_the_reconcile_command_checks_and_stores_yesterday(engine, capsys) -> None:
    await add(engine, ledger_row(1, at(1)))
    samples = {"llama-server-0": [[(START.timestamp() - 10, 0.0), (at(2).timestamp(), 40.0)]]}

    code = await run_reconcile(now=at(26), transport=prometheus(samples))

    assert code == 0
    out = capsys.readouterr().out
    assert "reconcile 2026-11-07: output tokens" in out
    assert "llama-server-0" in out and "exact" in out
    async with engine.connect() as conn:
        assert len((await conn.execute(select(ReconciliationRow.__table__))).all()) == 2


async def test_the_reconcile_command_fails_on_a_gap_and_says_what_it_means(engine, capsys) -> None:
    samples = {"llama-server-0": [[(START.timestamp() - 10, 0.0), (at(2).timestamp(), 40.0)]]}

    code = await run_reconcile("--day", "2026-11-07", now=at(26), transport=prometheus(samples))

    assert code == 1
    assert "llama-server-0: the server generated 40 tokens the ledger doesn't have" in (
        capsys.readouterr().err
    )


async def test_a_window_is_reconciled_without_storing_it(engine, capsys) -> None:
    samples = {"llama-server-0": [[(at(1).timestamp(), 0.0), (at(1.5).timestamp(), 0.0)]]}

    code = await run_reconcile("--start", "2026-11-07T00:30:00", "--end", "2026-11-07T02:00:00",
                               now=at(26), transport=prometheus(samples))  # fmt: skip

    assert code == 0
    assert "(not stored)" in capsys.readouterr().out
    async with engine.connect() as conn:
        assert (await conn.execute(select(ReconciliationRow.__table__))).all() == []


async def test_a_day_that_has_not_ended_is_refused(engine, capsys) -> None:
    code = await run_reconcile("--day", "2026-11-07", now=at(23), transport=prometheus({}))

    assert code == 1
    assert "2026-11-07 hasn't ended yet" in capsys.readouterr().err


async def test_the_check_requests_command_reads_client_records_on_stdin(
    engine, capsys, monkeypatch
) -> None:
    import io

    from tollgate_billing.__main__ import check_requests_command

    await add(engine, ledger_row(1, at(1)))
    lines = (f'{{"request_id": "{1:032x}", "tenant": "support", "outcome": "completed", '
             f'"input_tokens": 75, "output_tokens": 40, "chunks": 40}}\n'
             f'{{"request_id": "{2:032x}", "tenant": "support", "outcome": "completed", '
             f'"input_tokens": 75, "output_tokens": 40, "chunks": 40}}\n')  # fmt: skip
    monkeypatch.setattr("sys.stdin", io.StringIO(lines))

    assert await check_requests_command(settings()) == 1
    captured = capsys.readouterr()
    assert "2 requests: 1 completed and billed exactly as the client saw" in captured.out
    assert f"{2:032x}: completed but not in the ledger" in captured.err
