"""Monthly invoices in Postgres (ADR 0017): preview, finalize, show, list and verify.

A month's invoice holds the ledger rows *recorded* in that month, like a card statement goes by
posting date. recorded_at is set by Postgres when billing writes the row, so once a month has
ended no new row can be recorded in it, and a finalized invoice never needs to change.

Finalizing is safe to repeat, or to run twice at once: an existing invoice is left alone, and
invoice_items' primary key (the ledger row) keeps any row off a second invoice.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, and_, func, insert, literal, not_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tollgate_billing.db import InvoiceItemRow, InvoiceLineRow, InvoiceRow, LedgerRow
from tollgate_billing.invoicing import Draft, Line, ModelUsage, draft_invoice
from tollgate_billing.months import Month
from tollgate_billing.prices import PriceList


class InvoiceError(Exception):
    pass


@dataclass(frozen=True)
class Invoice:
    """A finalized invoice as stored. `total` is the stored total; draft.total sums the lines."""

    draft: Draft
    total: Decimal
    finalized_at: datetime


@dataclass(frozen=True)
class FinalizeResult:
    month: Month
    finalized: tuple[Draft, ...]
    already: tuple[str, ...]  # tenants whose invoice for the month already existed
    unpriced: dict[str, tuple[str, ...]]  # tenants left without one: their models with no price


def _recorded_in(month: Month):
    return and_(LedgerRow.recorded_at >= month.start, LedgerRow.recorded_at < month.end)


async def _usage(conn: AsyncConnection, month: Month,
                 tenant_id: str | None = None) -> dict[str, list[ModelUsage]]:  # fmt: skip
    """Billed tokens per tenant and model, over the ledger rows recorded in the month."""
    query = (
        select(
            LedgerRow.tenant_id,
            LedgerRow.model,
            func.count(),
            func.sum(LedgerRow.billable_input_tokens),
            func.sum(LedgerRow.billable_output_tokens),
        )
        .where(_recorded_in(month))
        .group_by(LedgerRow.tenant_id, LedgerRow.model)
    )
    if tenant_id is not None:
        query = query.where(LedgerRow.tenant_id == tenant_id)
    usage: dict[str, list[ModelUsage]] = defaultdict(list)
    for tenant, model, requests, tokens_in, tokens_out in (await conn.execute(query)).all():
        usage[tenant].append(ModelUsage(model, requests, int(tokens_in), int(tokens_out)))
    return usage


async def preview(engine: AsyncEngine, prices: PriceList, tenant_id: str, month: Month) -> Draft:
    """The tenant's invoice for the month so far. Not stored."""
    async with engine.connect() as conn:
        usage = await _usage(conn, month, tenant_id)
    return draft_invoice(tenant_id, month, usage.get(tenant_id, []), prices)


async def finalize(engine: AsyncEngine, prices: PriceList, month: Month, *,
                   now: datetime) -> FinalizeResult:  # fmt: skip
    """Store every tenant's invoice for a month that has ended, in one transaction."""
    if now < month.end:
        raise InvoiceError(f"{month} hasn't ended: it can be finalized from "
                           f"{month.end:%Y-%m-%d %H:%M} UTC")  # fmt: skip
    finalized, already, unpriced = [], [], {}
    async with engine.begin() as conn:
        # A ledger write that began before the month ended may not have committed yet, and its rows
        # are this month's. This lock waits for every write in flight to commit, and holds new ones
        # back until this transaction ends (they'd be next month's anyway). It conflicts with
        # itself, so a second finalize waits here too, then finds the invoices already made.
        # .NET: like taking a table lock in SQL Server; billing's writer just waits a moment.
        await conn.execute(text("LOCK TABLE usage_ledger IN SHARE ROW EXCLUSIVE MODE"))
        done = set(
            (
                await conn.execute(
                    select(InvoiceRow.tenant_id).where(InvoiceRow.period == month.first_day)
                )
            ).scalars()
        )
        for tenant, usage in sorted((await _usage(conn, month)).items()):
            if tenant in done:
                already.append(tenant)
                continue
            draft = draft_invoice(tenant, month, usage, prices)
            if draft.unpriced:  # never bill a model at zero because its price is missing
                unpriced[tenant] = draft.unpriced
                continue
            await _store(conn, draft)
            finalized.append(draft)
    return FinalizeResult(month, tuple(finalized), tuple(already), unpriced)


async def _store(conn: AsyncConnection, draft: Draft) -> None:
    period = draft.month.first_day
    await conn.execute(
        insert(InvoiceRow).values(
            tenant_id=draft.tenant_id, period=period, currency=draft.currency,
            requests=draft.requests, input_tokens=draft.input_tokens,
            output_tokens=draft.output_tokens, total=draft.total,
        )
    )  # fmt: skip
    if draft.lines:
        await conn.execute(
            insert(InvoiceLineRow),
            [
                {"tenant_id": draft.tenant_id, "period": period, "line_no": n,
                 "model": line.model, "meter": line.meter, "band_from": line.band_from,
                 "band_up_to": line.band_up_to, "tokens": line.tokens,
                 "per_million": line.per_million, "amount": line.amount}
                for n, line in enumerate(draft.lines, start=1)
            ],
        )  # fmt: skip
    linked = await conn.execute(
        insert(InvoiceItemRow).from_select(
            ["idempotency_key", "tenant_id", "period"],
            select(LedgerRow.idempotency_key, LedgerRow.tenant_id, literal(period, Date)).where(
                LedgerRow.tenant_id == draft.tenant_id, _recorded_in(draft.month)
            ),
        )
    )
    if linked.rowcount != draft.requests:  # the lock makes this impossible; never store it if not
        raise InvoiceError(f"{draft.tenant_id} {draft.month}: {linked.rowcount} ledger rows for "
                           f"{draft.requests} requests")  # fmt: skip


async def _stored(conn: AsyncConnection, month: Month | None = None,
                  tenant_id: str | None = None) -> list[Invoice]:  # fmt: skip
    invoices = select(InvoiceRow.__table__).order_by(InvoiceRow.period, InvoiceRow.tenant_id)
    lines = select(InvoiceLineRow.__table__).order_by(InvoiceLineRow.line_no)
    if month is not None:
        invoices = invoices.where(InvoiceRow.period == month.first_day)
        lines = lines.where(InvoiceLineRow.period == month.first_day)
    if tenant_id is not None:
        invoices = invoices.where(InvoiceRow.tenant_id == tenant_id)
        lines = lines.where(InvoiceLineRow.tenant_id == tenant_id)
    by_invoice: dict[tuple[str, date], list[Line]] = defaultdict(list)
    for r in (await conn.execute(lines)).all():
        by_invoice[r.tenant_id, r.period].append(
            Line(r.model, r.meter, r.band_from, r.band_up_to, r.tokens, r.per_million, r.amount)
        )
    return [
        Invoice(
            Draft(
                tenant_id=r.tenant_id,
                month=Month(r.period.year, r.period.month),
                currency=r.currency,
                requests=r.requests,
                input_tokens=r.input_tokens,
                output_tokens=r.output_tokens,
                lines=tuple(by_invoice[r.tenant_id, r.period]),
            ),
            total=r.total,
            finalized_at=r.finalized_at,
        )  # fmt: skip
        for r in (await conn.execute(invoices)).all()
    ]


async def stored_invoice(engine: AsyncEngine, tenant_id: str, month: Month) -> Invoice | None:
    async with engine.connect() as conn:
        found = await _stored(conn, month, tenant_id)
    return found[0] if found else None


async def list_invoices(engine: AsyncEngine, month: Month | None = None) -> list[Invoice]:
    async with engine.connect() as conn:
        return await _stored(conn, month)


async def verify(engine: AsyncEngine, prices: PriceList, month: Month) -> list[str]:
    """Check a finalized month against the ledger and the price list. Returns the problems found:
    an empty list means every ledger row recorded in the month is on exactly one invoice, and each
    invoice is exactly what its rows and the month's prices make."""
    problems = []
    async with engine.connect() as conn:
        invoices = {i.draft.tenant_id: i for i in await _stored(conn, month)}
        usage = await _usage(conn, month)

        for tenant, models in sorted(usage.items()):
            if tenant not in invoices:
                rows = sum(u.requests for u in models)
                problems.append(f"{tenant}: no invoice for {month}, which has {rows} of its "
                                f"ledger rows")  # fmt: skip

        missing = (
            select(LedgerRow.tenant_id, LedgerRow.idempotency_key)
            .join(
                InvoiceRow,
                and_(
                    InvoiceRow.tenant_id == LedgerRow.tenant_id,
                    InvoiceRow.period == month.first_day,
                ),
            )  # fmt: skip
            .outerjoin(InvoiceItemRow, InvoiceItemRow.idempotency_key == LedgerRow.idempotency_key)
            .where(_recorded_in(month), InvoiceItemRow.idempotency_key.is_(None))
            .order_by(LedgerRow.tenant_id, LedgerRow.idempotency_key)
        )
        for tenant, key in (await conn.execute(missing)).all():
            problems.append(f"{tenant}: ledger row {key} recorded in {month} is not on its invoice")

        stray = (
            select(InvoiceItemRow.tenant_id, InvoiceItemRow.idempotency_key)
            .join(LedgerRow, LedgerRow.idempotency_key == InvoiceItemRow.idempotency_key)
            .where(
                InvoiceItemRow.period == month.first_day,
                or_(LedgerRow.tenant_id != InvoiceItemRow.tenant_id, not_(_recorded_in(month))),
            )
        )
        for tenant, key in (await conn.execute(stray)).all():
            problems.append(f"{tenant}: ledger row {key} is on the {month} invoice but is another "
                            f"tenant's or month's")  # fmt: skip

        linked = dict(
            (
                await conn.execute(
                    select(InvoiceItemRow.tenant_id, func.count())
                    .where(InvoiceItemRow.period == month.first_day)
                    .group_by(InvoiceItemRow.tenant_id)
                )
            ).all()
        )

    for tenant, invoice in sorted(invoices.items()):
        stored = invoice.draft
        expected = draft_invoice(tenant, month, usage.get(tenant, []), prices)
        where = f"{tenant} {month}: the invoice doesn't match"
        if linked.get(tenant, 0) != stored.requests:
            problems.append(f"{where} its {linked.get(tenant, 0)} linked ledger rows "
                            f"({stored.requests} requests)")  # fmt: skip
        if invoice.total != stored.total:
            problems.append(f"{where} its own lines (total {invoice.total}, lines {stored.total})")
        differences = [
            f"{name} {getattr(stored, name)} vs {getattr(expected, name)}"
            for name in ("currency", "requests", "input_tokens", "output_tokens")
            if getattr(stored, name) != getattr(expected, name)
        ]
        if stored.lines != expected.lines:
            pairs = list(zip(stored.lines, expected.lines, strict=False))
            first = next((n for n, (a, b) in enumerate(pairs, 1) if a != b), len(pairs) + 1)
            differences.append(f"lines differ from line {first}")
        if differences:
            problems.append(f"{where} the ledger and price list ({'; '.join(differences)})")
    return problems
