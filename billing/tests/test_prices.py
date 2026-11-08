"""The price list (ADR 0017): read from TOML, refused when anything is off, and the band maths."""

from decimal import Decimal
from pathlib import Path

import pytest
from tollgate_billing.months import Month
from tollgate_billing.prices import Band, Charge, PriceFileError, charge, load_prices, parse_prices

D = Decimal

PRICES = """
currency = "USD"

[[prices]]
model = "smollm3-3b"
from = "2026-11"
input = [{ per_million = "0.10" }]
output = [
  { up_to = 10_000_000, per_million = "0.40" },
  { per_million = "0.30" },
]

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
"""


def test_a_price_list_reads_rates_as_decimals_never_floats() -> None:
    prices = parse_prices(PRICES)

    assert prices.currency == "USD"
    smollm = prices.for_model("smollm3-3b", Month(2026, 11))
    assert smollm.input == (Band(None, D("0.10")),)
    assert smollm.output == (Band(10_000_000, D("0.40")), Band(None, D("0.30")))
    assert isinstance(smollm.output[0].per_million, Decimal)


@pytest.mark.parametrize(
    ("month", "input_rate"),
    [(Month(2026, 11), "0.10"), (Month(2026, 12), "0.10"), (Month(2027, 1), "0.08"),
     (Month(2027, 6), "0.08")],
)  # fmt: skip
def test_the_latest_version_that_has_started_applies(month, input_rate) -> None:
    price = parse_prices(PRICES).for_model("smollm3-3b", month)

    assert price.input[0].per_million == D(input_rate)


def test_no_price_before_a_models_first_version_or_for_an_unknown_model() -> None:
    prices = parse_prices(PRICES)

    assert prices.for_model("claude-opus-5-5", Month(2026, 10)) is None
    assert prices.for_model("mystery-model", Month(2026, 11)) is None


def one_price(**parts: str) -> str:
    """A price file with one entry; each keyword replaces one part of it."""
    p = {
        "currency": '"USD"',
        "model": '"m"',
        "from": '"2026-11"',
        "input": '[{ per_million = "0.10" }]',
        "output": '[{ per_million = "0.40" }]',
        "extra": "",
    }
    p.update(parts)
    return (f"currency = {p['currency']}\n[[prices]]\nmodel = {p['model']}\nfrom = {p['from']}\n"
            f"input = {p['input']}\noutput = {p['output']}\n{p['extra']}")  # fmt: skip


@pytest.mark.parametrize(
    ("parts", "message"),
    [
        ({"input": "[{ per_million = 0.10 }]"}, 'a string like "0.20"'),
        ({"input": '[{ per_million = "NaN" }]'}, "not a price"),
        ({"input": '[{ per_million = "abc" }]'}, "not a price"),
        ({"input": '[{ per_million = "-0.10" }]'}, "negative"),
        ({"input": '[{ per_million = "0.1234567" }]'}, "6 decimal places"),
        ({"input": '[{ per_million = "1000000" }]'}, "too large"),
        ({"input": "[{ up_to = 10 }]"}, "missing per_million"),
        ({"input": "[]"}, "at least one band"),
        ({"input": '"0.10"'}, "a list of bands"),
        ({"output": '[{ up_to = 10, per_million = "0.40" }]'}, "last band has no up_to"),
        ({"output": '[{ per_million = "0.40" }, { per_million = "0.30" }]'}, "needs up_to"),
        ({"output": '[{ up_to = 20, per_million = "0.4" }, { up_to = 10, per_million = "0.3" },'
                    ' { per_million = "0.2" }]'}, "more than the band before"),
        ({"output": '[{ up_to = 0, per_million = "0.4" }, { per_million = "0.3" }]'},
         "more than 0"),
        ({"output": '[{ up_to = true, per_million = "0.4" }, { per_million = "0.3" }]'},
         "whole number"),
        ({"output": '[{ up_to = 1e7, per_million = "0.4" }, { per_million = "0.3" }]'},
         "whole number"),
        ({"output": '[{ per_milion = "0.40" }]'}, "unknown key per_milion"),
        ({"extra": 'tier = "gold"'}, "unknown key tier"),
        ({"currency": '"usd"'}, "currency"),
        ({"from": '"2026-13"'}, "YYYY-MM"),
        ({"from": "2026-11-01"}, "YYYY-MM"),
        ({"model": '""'}, "model"),
        ({"extra": '[[prices]]\nmodel = "m"\nfrom = "2026-11"\ninput = [{ per_million = "1" }]\n'
                   'output = [{ per_million = "1" }]'}, "twice"),
        ({"currency": '"USD"\nnot toml'}, "TOML"),
    ],
)  # fmt: skip
def test_a_price_file_with_anything_off_is_refused(parts, message) -> None:
    with pytest.raises(PriceFileError, match=message):
        parse_prices(one_price(**parts))


def test_a_price_file_needs_prices_and_every_part_of_each() -> None:
    with pytest.raises(PriceFileError, match="no prices"):
        parse_prices('currency = "USD"\n')
    with pytest.raises(PriceFileError, match="missing output"):
        parse_prices('currency = "USD"\n[[prices]]\nmodel = "m"\nfrom = "2026-11"\n'
                     'input = [{ per_million = "1" }]\n')  # fmt: skip


def test_the_price_list_in_the_repo_prices_every_model_the_gateway_answers_with() -> None:
    deploy = Path(__file__).parents[2] / "deploy"

    def setting(path: Path, name: str) -> str:
        for line in path.read_text().splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
        raise AssertionError(f"{name} not in {path}")

    models = {
        setting(deploy / "llama-server" / "llama-server.env", "LLAMA_ARG_ALIAS"),  # also vLLM's
        setting(deploy / "gateway" / "gateway.env", "TOLLGATE_HOSTED_MODEL"),
    }
    prices = load_prices(deploy / "billing" / "prices.toml")

    assert prices.currency == "USD"
    for model in models:
        assert prices.for_model(model, Month(2026, 11)) is not None, model


# --- the band maths -------------------------------------------------------------------------------


def test_one_band_charges_every_token_at_its_rate() -> None:
    assert charge(2_500_000, (Band(None, D("0.10")),)) == [
        Charge(0, None, 2_500_000, D("0.10"), D("0.25"))
    ]


def test_tokens_exactly_at_a_band_edge_stay_in_that_band() -> None:
    bands = (Band(1_000_000, D("0.40")), Band(None, D("0.30")))

    assert charge(1_000_000, bands) == [Charge(0, 1_000_000, 1_000_000, D("0.40"), D("0.40"))]
    assert charge(1_000_001, bands) == [
        Charge(0, 1_000_000, 1_000_000, D("0.40"), D("0.40")),
        Charge(1_000_000, None, 1, D("0.30"), D("0.00")),
    ]


def test_graduated_bands_charge_each_part_at_its_own_rate() -> None:
    bands = (Band(1_000_000, D("3.00")), Band(3_000_000, D("2.00")), Band(None, D("1.00")))

    assert charge(4_500_000, bands) == [
        Charge(0, 1_000_000, 1_000_000, D("3.00"), D("3.00")),
        Charge(1_000_000, 3_000_000, 2_000_000, D("2.00"), D("4.00")),
        Charge(3_000_000, None, 1_500_000, D("1.00"), D("1.50")),
    ]


def test_no_tokens_no_charge() -> None:
    assert charge(0, (Band(None, D("0.40")),)) == []


@pytest.mark.parametrize(
    ("tokens", "rate", "amount"),
    [
        (10_000, "0.50", "0.01"),  # exactly 0.005: half-up gives 0.01 (banker's rounding: 0.00)
        (50_000, "0.50", "0.03"),  # exactly 0.025: half-up gives 0.03 (banker's rounding: 0.02)
        (8_000, "0.50", "0.00"),  # 0.004
        (1, "4.40", "0.00"),  # 0.0000044
    ],
)
def test_each_charge_is_rounded_once_to_the_cent_half_up(tokens, rate, amount) -> None:
    [line] = charge(tokens, (Band(None, D(rate)),))

    assert line.amount == D(amount)
    assert str(line.amount) == amount  # always two decimal places
