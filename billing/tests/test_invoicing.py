"""Months and invoice drafts (ADR 0017): which month a row is in, and how usage becomes lines."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from tollgate_billing.invoicing import Line, ModelUsage, describe, draft_invoice, rate_text
from tollgate_billing.months import Month
from tollgate_billing.prices import parse_prices

D = Decimal

PRICES = parse_prices("""
currency = "USD"

[[prices]]
model = "smollm3-3b"
from = "2026-11"
input = [{ per_million = "0.10" }]
output = [{ up_to = 10_000_000, per_million = "0.40" }, { per_million = "0.30" }]

[[prices]]
model = "smollm3-3b"
from = "2027-01"
input = [{ per_million = "0.08" }]
output = [{ per_million = "0.35" }]

[[prices]]
model = "claude-opus-5-5"
from = "2026-11"
input = [{ per_million = "4.40" }]
output = [{ per_million = "22.00" }]

[[prices]]
model = "half-cent"
from = "2026-11"
input = [{ per_million = "0.50" }]
output = [{ per_million = "0.50" }]
""")

# --- months ---------------------------------------------------------------------------------------


def test_a_month_reads_and_prints_as_year_dash_month() -> None:
    assert Month.parse("2026-11") == Month(2026, 11)
    assert str(Month(2026, 1)) == "2026-01"


@pytest.mark.parametrize("text", ["2026-1", "2026-13", "2026-00", "26-11", "2026-11-01", "Nov"])
def test_anything_but_year_dash_month_is_refused(text) -> None:
    with pytest.raises(ValueError, match="YYYY-MM"):
        Month.parse(text)


def test_a_month_runs_from_its_first_instant_to_the_next_months_in_utc() -> None:
    assert Month(2026, 11).start == datetime(2026, 11, 1, tzinfo=UTC)
    assert Month(2026, 11).end == datetime(2026, 12, 1, tzinfo=UTC)
    assert Month(2026, 12).end == datetime(2027, 1, 1, tzinfo=UTC)
    assert Month(2027, 1).previous() == Month(2026, 12)
    assert Month(2026, 12).next() == Month(2027, 1)


def test_the_month_of_an_instant_is_taken_in_utc() -> None:
    assert Month.of(datetime(2026, 11, 30, 23, 59, 59, 999999, tzinfo=UTC)) == Month(2026, 11)
    assert Month.of(datetime(2026, 12, 1, tzinfo=UTC)) == Month(2026, 12)
    # 00:30 on 1 December in Paris is still November in UTC
    assert Month.of(datetime(2026, 12, 1, 0, 30, tzinfo=timezone(timedelta(hours=1)))) == Month(
        2026, 11
    )


# --- drafts ---------------------------------------------------------------------------------------

USAGE = [
    ModelUsage("smollm3-3b", requests=7, input_tokens=2_500_000, output_tokens=12_000_000),
    ModelUsage("claude-opus-5-5", requests=2, input_tokens=100_000, output_tokens=50_000),
]


def test_usage_becomes_one_line_per_model_meter_and_band() -> None:
    draft = draft_invoice("support", Month(2026, 11), USAGE, PRICES)

    assert draft.lines == (
        Line("claude-opus-5-5", "input", 0, None, 100_000, D("4.40"), D("0.44")),
        Line("claude-opus-5-5", "output", 0, None, 50_000, D("22.00"), D("1.10")),
        Line("smollm3-3b", "input", 0, None, 2_500_000, D("0.10"), D("0.25")),
        Line("smollm3-3b", "output", 0, 10_000_000, 10_000_000, D("0.40"), D("4.00")),
        Line("smollm3-3b", "output", 10_000_000, None, 2_000_000, D("0.30"), D("0.60")),
    )
    assert draft.total == D("6.39")
    assert (draft.requests, draft.input_tokens, draft.output_tokens) == (9, 2_600_000, 12_050_000)
    assert (draft.tenant_id, draft.month, draft.currency) == ("support", Month(2026, 11), "USD")
    assert draft.unpriced == ()


def test_the_months_own_price_version_applies() -> None:
    draft = draft_invoice("support", Month(2027, 1), USAGE[:1], PRICES)

    assert [(line.meter, line.per_million) for line in draft.lines] == [
        ("input", D("0.08")),
        ("output", D("0.35")),
    ]


def test_the_total_is_the_sum_of_the_rounded_lines() -> None:
    # Each line is exactly 0.005 and rounds to 0.01; the exact sum would be 0.01.
    usage = [ModelUsage("half-cent", requests=1, input_tokens=10_000, output_tokens=10_000)]

    draft = draft_invoice("support", Month(2026, 11), usage, PRICES)

    assert [line.amount for line in draft.lines] == [D("0.01"), D("0.01")]
    assert draft.total == D("0.02")


def test_a_model_with_no_price_is_reported_and_the_rest_still_priced() -> None:
    usage = [*USAGE, ModelUsage("mystery-model", requests=1, input_tokens=10, output_tokens=5)]

    draft = draft_invoice("support", Month(2026, 11), usage, PRICES)

    assert draft.unpriced == ("mystery-model",)
    assert {line.model for line in draft.lines} == {"claude-opus-5-5", "smollm3-3b"}
    assert draft.requests == 10


def test_a_model_used_before_its_first_price_is_unpriced() -> None:
    draft = draft_invoice("engineering", Month(2026, 10), USAGE[1:], PRICES)

    assert draft.unpriced == ("claude-opus-5-5",)


def test_requests_billed_at_nothing_need_no_price_and_make_no_lines() -> None:
    # Only failed requests: billed at zero tokens (policy.py), so nothing to price.
    usage = [ModelUsage("mystery-model", requests=3, input_tokens=0, output_tokens=0)]

    draft = draft_invoice("support", Month(2026, 11), usage, PRICES)

    assert (draft.lines, draft.unpriced, draft.total, draft.requests) == ((), (), D("0.00"), 3)


def test_no_usage_is_an_empty_draft() -> None:
    draft = draft_invoice("support", Month(2026, 11), [], PRICES)

    assert (draft.lines, draft.total, draft.requests) == ((), D("0.00"), 0)


def test_describe_shows_lines_bands_and_the_total() -> None:
    text = "\n".join(describe(draft_invoice("support", Month(2026, 11), USAGE, PRICES)))

    assert "invoice 2026-11 support" in text
    assert "up to 10,000,000" in text
    assert "over 10,000,000" in text
    assert "total USD 6.39 for 9 requests (2,600,000 in, 12,050,000 out)" in text


@pytest.mark.parametrize(
    ("rate", "shown"),
    [("20.00", "20.00"), ("22", "22.00"), ("0.100000", "0.10"), ("0.4", "0.40"),
     ("0.000001", "0.000001"), ("4.125", "4.125")],
)  # fmt: skip
def test_rates_show_at_least_cents_and_never_in_scientific_notation(rate, shown) -> None:
    assert rate_text(D(rate)) == shown
