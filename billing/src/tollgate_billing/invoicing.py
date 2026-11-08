"""Invoice drafts (ADR 0017): a tenant's billed tokens in a month, priced into lines. Pure: no
database. invoices.py feeds it the ledger and stores what it returns.

A line is one model, one meter (input or output tokens) and one price band. Each line is rounded
once to the cent; the total is the sum of the lines, so the lines always add up to it.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from tollgate_billing.months import Month
from tollgate_billing.prices import PriceList, charge

ZERO = Decimal("0.00")
METERS = ("input", "output")


@dataclass(frozen=True)
class ModelUsage:
    """One tenant's ledger rows for one model in a month: the billed tokens, summed."""

    model: str
    requests: int
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class Line:
    model: str
    meter: str  # "input" or "output"
    band_from: int
    band_up_to: int | None
    tokens: int
    per_million: Decimal
    amount: Decimal


@dataclass(frozen=True)
class Draft:
    tenant_id: str
    month: Month
    currency: str
    requests: int
    input_tokens: int
    output_tokens: int
    lines: tuple[Line, ...]
    unpriced: tuple[str, ...] = ()  # models used this month that have no price for it

    @property
    def total(self) -> Decimal:
        return sum((line.amount for line in self.lines), ZERO)


def draft_invoice(tenant_id: str, month: Month, usage: Iterable[ModelUsage],
                  prices: PriceList) -> Draft:  # fmt: skip
    usage = sorted(usage, key=lambda u: u.model)
    lines, unpriced = [], []
    for u in usage:
        price = prices.for_model(u.model, month)
        if price is None:
            # Requests billed at nothing (failed ones) cost nothing at any price.
            if u.input_tokens or u.output_tokens:
                unpriced.append(u.model)
            continue
        for meter, tokens, bands in (("input", u.input_tokens, price.input),
                                     ("output", u.output_tokens, price.output)):  # fmt: skip
            lines += [
                Line(u.model, meter, c.band_from, c.band_up_to, c.tokens, c.per_million, c.amount)
                for c in charge(tokens, bands)
            ]
    return Draft(
        tenant_id=tenant_id,
        month=month,
        currency=prices.currency,
        requests=sum(u.requests for u in usage),
        input_tokens=sum(u.input_tokens for u in usage),
        output_tokens=sum(u.output_tokens for u in usage),
        lines=tuple(lines),
        unpriced=tuple(unpriced),
    )


def rate_text(per_million: Decimal) -> str:
    """A rate with at least two decimals: "0.10", "22.00", "0.000001". (Decimal.normalize alone
    would print 20.00 as "2E+1".)"""
    whole, _, fraction = f"{per_million.normalize():f}".partition(".")
    return f"{whole}.{fraction.ljust(2, '0')}"


def band_label(band_from: int, band_up_to: int | None) -> str:
    if band_up_to is None:
        return "all" if band_from == 0 else f"over {band_from:,}"
    return f"up to {band_up_to:,}" if band_from == 0 else f"{band_from:,} to {band_up_to:,}"


def describe(draft: Draft, finalized_at: datetime | None = None) -> list[str]:
    """The invoice as text, for the command line."""
    state = (f"finalized {finalized_at:%Y-%m-%d %H:%M} UTC" if finalized_at
             else "draft: not final until the month ends")  # fmt: skip
    out = [f"invoice {draft.month} {draft.tenant_id} ({state})"]
    if draft.lines:
        out.append(f"  {'model':<18}{'meter':<8}{'band':<24}{'tokens':>14}"
                   f"{'per 1M':>12}{'amount':>12}")  # fmt: skip
        for line in draft.lines:
            out.append(
                f"  {line.model:<18}{line.meter:<8}"
                f"{band_label(line.band_from, line.band_up_to):<24}{line.tokens:>14,}"
                f"{rate_text(line.per_million):>12}{line.amount:>12}"
            )
    for model in draft.unpriced:
        out.append(f"  {model}: no price for {draft.month}; add one to prices.toml")
    out.append(f"  total {draft.currency} {draft.total} for {draft.requests:,} requests "
               f"({draft.input_tokens:,} in, {draft.output_tokens:,} out)")  # fmt: skip
    return out
