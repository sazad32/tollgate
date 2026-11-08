"""Invoices against a real Postgres (ADR 0017): finalize, preview and verify.

A month's invoice holds the ledger rows *recorded* in that month. These tests write ledger rows
directly, with recorded_at set by hand, to stand for rows recorded at that moment.
Needs TOLLGATE_TEST_DATABASE_URL, like test_ledger_postgres.py.
"""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, inspect, select, text
from sqlalchemy.exc import IntegrityError
from tollgate_billing.db import InvoiceItemRow, InvoiceRow, LedgerRow, make_engine
from tollgate_billing.invoices import (
    InvoiceError,
    finalize,
    list_invoices,
    preview,
    stored_invoice,
    verify,
)
from tollgate_billing.migrate import downgrade, upgrade
from tollgate_billing.months import Month
from tollgate_billing.prices import parse_prices

URL = os.environ.get("TOLLGATE_TEST_DATABASE_URL", "")
pytestmark = [
    pytest.mark.skipif(not URL, reason="set TOLLGATE_TEST_DATABASE_URL to run the Postgres tests"),
    pytest.mark.asyncio,
]

OCTOBER = Month(2026, 10)
AFTER_OCTOBER = datetime(2026, 11, 1, 6, tzinfo=UTC)  # when the CronJob runs

PRICE_TEXT = """
currency = "USD"

[[prices]]
model = "smollm3-3b"
from = "2026-01"
input = [{ per_million = "0.10" }]
output = [{ up_to = 10_000_000, per_million = "0.40" }, { per_million = "0.30" }]

[[prices]]
model = "claude-opus-5-5"
from = "2026-01"
input = [{ per_million = "4.40" }]
output = [{ per_million = "22.00" }]
"""
PRICES = parse_prices(PRICE_TEXT)


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
        await conn.execute(text("TRUNCATE usage_ledger, usage_rejects, invoices CASCADE"))
    yield engine
    await engine.dispose()


def ledger_row(n: int, recorded_at: datetime, *, tenant: str = "support",
               model: str = "smollm3-3b", tokens: tuple[int, int] = (2_000_000, 3_000_000),
               status: str = "completed", started_at: datetime | None = None) -> dict:  # fmt: skip
    billed = (0, 0) if status == "failed" else tokens
    started = started_at or recorded_at
    return {
        "idempotency_key": f"{n:032x}:usage", "request_id": f"{n:032x}", "tenant_id": tenant,
        "credential": "key:test", "model": model, "backend": "llama-server-0",
        "input_tokens": tokens[0], "output_tokens": tokens[1], "token_source": "reported",
        "status": status, "stream": False,
        "billable_input_tokens": billed[0], "billable_output_tokens": billed[1],
        "started_at": started, "finished_at": started, "schema_version": 1,
        "stream_id": f"{n}-0", "recorded_at": recorded_at,
    }  # fmt: skip


async def add(engine, *rows: dict) -> None:
    async with engine.begin() as conn:
        await conn.execute(insert(LedgerRow), list(rows))


def oct_(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=UTC)


async def october_usage(engine) -> None:
    """Three support rows and one engineering row in October, plus rows just outside it."""
    await add(
        engine,
        ledger_row(1, oct_(1, 0)),
        ledger_row(2, oct_(15)),
        ledger_row(3, datetime(2026, 10, 31, 23, 59, 59, 999999, tzinfo=UTC)),
        ledger_row(4, oct_(20), tenant="engineering", model="claude-opus-5-5",
                   tokens=(100_000, 50_000)),
        ledger_row(5, datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC)),  # September's
        # started in October, recorded in November: November's (billed when metered)
        ledger_row(6, datetime(2026, 11, 1, 0, 0, 1, tzinfo=UTC),
                   started_at=datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC)),
    )  # fmt: skip


async def count(engine, table) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(select(func.count()).select_from(table))).scalar_one()


# --- finalize -------------------------------------------------------------------------------------


async def test_finalize_invoices_each_tenant_for_the_rows_recorded_in_the_month(engine) -> None:
    await october_usage(engine)

    result = await finalize(engine, PRICES, OCTOBER, now=AFTER_OCTOBER)

    assert sorted(d.tenant_id for d in result.finalized) == ["engineering", "support"]
    support = (await stored_invoice(engine, "support", OCTOBER)).draft
    assert (support.requests, support.input_tokens, support.output_tokens) == (
        3, 6_000_000, 9_000_000,
    )  # fmt: skip
    # input 6M at 0.10 = 0.60; output 9M, all in the first band, at 0.40 = 3.60
    assert support.total == Decimal("4.20")
    engineering = (await stored_invoice(engine, "engineering", OCTOBER)).draft
    assert engineering.total == Decimal("1.54")  # 0.44 + 1.10
    assert await count(engine, InvoiceItemRow) == 4  # rows 1-4; not September's, not November's


async def test_a_month_that_has_not_ended_is_refused(engine) -> None:
    await october_usage(engine)

    with pytest.raises(InvoiceError, match="2026-10 hasn't ended"):
        await finalize(engine, PRICES, OCTOBER, now=datetime(2026, 10, 31, 23, 59, tzinfo=UTC))
    assert await count(engine, InvoiceRow) == 0

    result = await finalize(engine, PRICES, OCTOBER, now=datetime(2026, 11, 1, tzinfo=UTC))
    assert len(result.finalized) == 2


async def test_finalizing_again_changes_nothing(engine) -> None:
    await october_usage(engine)
    await finalize(engine, PRICES, OCTOBER, now=AFTER_OCTOBER)

    again = await finalize(engine, PRICES, OCTOBER, now=AFTER_OCTOBER)

    assert (again.finalized, again.already) == ((), ("engineering", "support"))
    assert (await count(engine, InvoiceRow), await count(engine, InvoiceItemRow)) == (2, 4)


async def test_two_finalizers_at_once_make_one_invoice_per_tenant(engine) -> None:
    await october_usage(engine)

    results = await asyncio.gather(
        *(finalize(engine, PRICES, OCTOBER, now=AFTER_OCTOBER) for _ in range(2))
    )

    assert sum(len(r.finalized) for r in results) == 2
    assert (await count(engine, InvoiceRow), await count(engine, InvoiceItemRow)) == (2, 4)


async def test_finalize_waits_for_a_ledger_write_still_in_flight(engine) -> None:
    # A batch that began in October and hasn't committed yet: its rows are October's, so the
    # invoice must wait for it rather than miss it.
    await october_usage(engine)
    async with engine.connect() as writer:
        await writer.begin()
        await writer.execute(insert(LedgerRow), [ledger_row(7, oct_(31, 23))])
        task = asyncio.create_task(finalize(engine, PRICES, OCTOBER, now=AFTER_OCTOBER))
        await asyncio.sleep(0.5)
        assert not task.done()
        await writer.commit()

    result = await asyncio.wait_for(task, timeout=10)

    support = next(d for d in result.finalized if d.tenant_id == "support")
    assert support.requests == 4


async def test_a_tenant_using_an_unpriced_model_waits_and_the_others_go_ahead(engine) -> None:
    await october_usage(engine)
    await add(engine, ledger_row(8, oct_(9), model="mystery-model"))

    first = await finalize(engine, PRICES, OCTOBER, now=AFTER_OCTOBER)

    assert [d.tenant_id for d in first.finalized] == ["engineering"]
    assert first.unpriced == {"support": ("mystery-model",)}
    assert await stored_invoice(engine, "support", OCTOBER) is None

    priced = parse_prices(
        PRICE_TEXT + '\n[[prices]]\nmodel = "mystery-model"\nfrom = "2026-01"\n'
        'input = [{ per_million = "1" }]\noutput = [{ per_million = "1" }]\n'
    )
    second = await finalize(engine, priced, OCTOBER, now=AFTER_OCTOBER)

    assert ([d.tenant_id for d in second.finalized], second.already) == (
        ["support"],
        ("engineering",),
    )
    assert (await stored_invoice(engine, "support", OCTOBER)).draft.requests == 4


async def test_failed_requests_are_on_the_invoice_at_nothing(engine) -> None:
    await add(engine, ledger_row(1, oct_(2)), ledger_row(2, oct_(3), status="failed"))

    await finalize(engine, PRICES, OCTOBER, now=AFTER_OCTOBER)

    draft = (await stored_invoice(engine, "support", OCTOBER)).draft
    assert (draft.requests, draft.input_tokens, draft.output_tokens) == (2, 2_000_000, 3_000_000)
    assert await count(engine, InvoiceItemRow) == 2


async def test_a_ledger_row_cannot_be_on_two_invoices(engine) -> None:
    await october_usage(engine)
    await finalize(engine, PRICES, OCTOBER, now=AFTER_OCTOBER)

    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                insert(InvoiceItemRow).values(
                    idempotency_key=f"{1:032x}:usage", tenant_id="engineering",
                    period=OCTOBER.first_day,
                )
            )  # fmt: skip


# --- preview, show, list --------------------------------------------------------------------------


async def test_a_preview_is_what_finalize_then_stores(engine) -> None:
    await october_usage(engine)
    before = await preview(engine, PRICES, "support", OCTOBER)

    await finalize(engine, PRICES, OCTOBER, now=AFTER_OCTOBER)

    stored = await stored_invoice(engine, "support", OCTOBER)
    assert stored.draft == before
    assert stored.finalized_at is not None


async def test_a_preview_of_a_tenant_with_no_usage_is_empty(engine) -> None:
    draft = await preview(engine, PRICES, "nobody", OCTOBER)

    assert (draft.requests, draft.lines, draft.total) == (0, (), Decimal("0.00"))


async def test_list_invoices_by_month(engine) -> None:
    await october_usage(engine)
    await finalize(engine, PRICES, OCTOBER, now=AFTER_OCTOBER)

    invoices = await list_invoices(engine, OCTOBER)

    assert [(i.draft.tenant_id, i.draft.total) for i in invoices] == [
        ("engineering", Decimal("1.54")),
        ("support", Decimal("4.20")),
    ]
    assert await list_invoices(engine, Month(2026, 9)) == []


# --- verify ---------------------------------------------------------------------------------------


async def test_verify_finds_nothing_wrong_with_finalized_invoices(engine) -> None:
    await october_usage(engine)
    await finalize(engine, PRICES, OCTOBER, now=AFTER_OCTOBER)

    assert await verify(engine, PRICES, OCTOBER) == []


async def test_verify_finds_a_ledger_row_missing_from_its_invoice(engine) -> None:
    await october_usage(engine)
    await finalize(engine, PRICES, OCTOBER, now=AFTER_OCTOBER)
    async with engine.begin() as conn:
        await conn.execute(
            text(f"DELETE FROM invoice_items WHERE idempotency_key = '{2:032x}:usage'")
        )

    problems = await verify(engine, PRICES, OCTOBER)

    assert any(f"{2:032x}:usage" in p and "not on" in p for p in problems), problems


async def test_verify_finds_an_invoice_that_no_longer_matches_the_ledger(engine) -> None:
    await october_usage(engine)
    await finalize(engine, PRICES, OCTOBER, now=AFTER_OCTOBER)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE invoice_lines SET tokens = tokens + 1 WHERE line_no = 1"))

    problems = await verify(engine, PRICES, OCTOBER)

    assert problems
    assert all("doesn't match" in p for p in problems), problems


async def test_verify_finds_an_invoice_priced_differently_from_the_price_list(engine) -> None:
    await october_usage(engine)
    await finalize(engine, PRICES, OCTOBER, now=AFTER_OCTOBER)
    changed = parse_prices(PRICE_TEXT.replace('"0.10"', '"0.11"'))

    problems = await verify(engine, changed, OCTOBER)

    assert [p for p in problems if "support" in p], problems


async def test_verify_finds_a_tenant_left_without_an_invoice(engine) -> None:
    await october_usage(engine)
    await add(engine, ledger_row(8, oct_(9), tenant="billing", model="mystery-model"))
    await finalize(engine, PRICES, OCTOBER, now=AFTER_OCTOBER)

    problems = await verify(engine, PRICES, OCTOBER)

    assert problems == ["billing: no invoice for 2026-10, which has 1 of its ledger rows"]


# --- schema ---------------------------------------------------------------------------------------


async def test_the_invoice_migration_runs_both_ways(engine) -> None:
    async def tables() -> set[str]:
        async with engine.connect() as conn:
            return set(await conn.run_sync(lambda c: inspect(c).get_table_names()))

    invoice_tables = {"invoices", "invoice_lines", "invoice_items"}
    await asyncio.to_thread(downgrade, URL, "0001")
    assert not invoice_tables & await tables()
    assert "usage_ledger" in await tables()
    await asyncio.to_thread(upgrade, URL)
    assert invoice_tables <= await tables()
