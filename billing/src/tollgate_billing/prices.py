"""The price list: what a tenant pays per million tokens, by model (ADR 0017).

It lives in a TOML file in Git (deploy/billing/prices.toml), so a price change is a reviewed pull
request. A model's price can be graduated: bands of the month's tokens, each at its own rate, like
income tax brackets. A version takes effect on the 1st of its month (UTC) and lasts until the next.

Money is Decimal, never float. .NET: Python's Decimal is C#'s `decimal`, and TOML floats are
binary doubles, so rates must be written as strings ("0.40") and are refused otherwise.
"""

import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from tollgate_billing.months import Month

MILLION = 1_000_000
CENT = Decimal("0.01")
MAX_RATE = Decimal(1_000_000)  # per million tokens; fits the invoice_lines column


class PriceFileError(ValueError):
    pass


@dataclass(frozen=True)
class Band:
    """Tokens up to `up_to` in the month (counting from the band before) at `per_million`.
    The last band has no end."""

    up_to: int | None
    per_million: Decimal


@dataclass(frozen=True)
class ModelPrice:
    model: str
    effective: Month
    input: tuple[Band, ...]
    output: tuple[Band, ...]


@dataclass(frozen=True)
class PriceList:
    currency: str
    prices: tuple[ModelPrice, ...]

    def for_model(self, model: str, month: Month) -> ModelPrice | None:
        """The model's latest version that has started by `month`, or None if it has no price."""
        started = [p for p in self.prices if p.model == model and p.effective <= month]
        return max(started, key=lambda p: p.effective, default=None)


@dataclass(frozen=True)
class Charge:
    band_from: int  # tokens before this band starts
    band_up_to: int | None
    tokens: int
    per_million: Decimal
    amount: Decimal  # rounded to the cent


def charge(tokens: int, bands: Sequence[Band]) -> list[Charge]:
    """Charge a month's tokens on one meter: each band's share at its own rate, each rounded once
    to the cent, half-up. A band with no tokens makes no charge."""
    charges = []
    start = 0
    for band in bands:
        end = tokens if band.up_to is None else min(tokens, band.up_to)
        if end > start:
            exact = Decimal(end - start) * band.per_million / MILLION
            # Explicit: Decimal's default (like C#'s Math.Round) is banker's rounding.
            amount = exact.quantize(CENT, rounding=ROUND_HALF_UP)
            charges.append(Charge(start, band.up_to, end - start, band.per_million, amount))
        if band.up_to is None or tokens <= band.up_to:
            break
        start = band.up_to
    return charges


# --- reading the file -----------------------------------------------------------------------------


def load_prices(path: Path) -> PriceList:
    return parse_prices(Path(path).read_text(encoding="utf-8"))


def parse_prices(text: str) -> PriceList:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise PriceFileError(f"not valid TOML: {e}") from None
    _keys(data, "the price file", required={"currency"}, optional={"prices"})
    currency = data["currency"]
    if not (isinstance(currency, str) and len(currency) == 3 and currency.isascii()
            and currency.isupper()):  # fmt: skip
        raise PriceFileError(f'currency must be a 3-letter code like "USD", not {currency!r}')
    entries = data.get("prices", [])
    if not isinstance(entries, list) or not entries:
        raise PriceFileError("no prices: add at least one [[prices]] table")

    prices = [_model_price(entry, f"prices[{i}]") for i, entry in enumerate(entries)]
    seen = set()
    for p in prices:
        if (p.model, p.effective) in seen:
            raise PriceFileError(f"{p.model} has a price from {p.effective} twice")
        seen.add((p.model, p.effective))
    return PriceList(currency, tuple(prices))


def _keys(table: object, where: str, *, required: set[str], optional: set[str] = frozenset()):
    if not isinstance(table, dict):
        raise PriceFileError(f"{where} must be a table")
    for key in table:
        if key not in required | optional:
            raise PriceFileError(f"{where}: unknown key {key}")
    for key in sorted(required - table.keys()):
        raise PriceFileError(f"{where}: missing {key}")


def _model_price(entry: object, where: str) -> ModelPrice:
    _keys(entry, where, required={"model", "from", "input", "output"})
    model = entry["model"]
    if not (isinstance(model, str) and 0 < len(model) <= 200 and model.isprintable()):
        raise PriceFileError(f"{where}: model must be the model's name, as the gateway reports it")
    where = f"{where} ({model})"
    try:
        effective = Month.parse(entry["from"]) if isinstance(entry["from"], str) else None
    except ValueError:
        effective = None
    if effective is None:
        raise PriceFileError(f'{where}: from must be a month as a string, YYYY-MM like "2026-11"')
    return ModelPrice(model, effective, _bands(entry["input"], f"{where}.input"),
                      _bands(entry["output"], f"{where}.output"))  # fmt: skip


def _bands(value: object, where: str) -> tuple[Band, ...]:
    if not isinstance(value, list):
        raise PriceFileError(f"{where} must be a list of bands")
    if not value:
        raise PriceFileError(f"{where} needs at least one band")
    bands = []
    for i, band in enumerate(value):
        here = f"{where}[{i}]"
        _keys(band, here, required={"per_million"}, optional={"up_to"})
        last = i == len(value) - 1
        up_to = band.get("up_to")
        if last and up_to is not None:
            raise PriceFileError(f"{here}: the last band has no up_to (it covers everything after)")
        if not last:
            if up_to is None:
                raise PriceFileError(f"{here} needs up_to: only the last band is open-ended")
            # bool is a kind of int in Python: `up_to = true` must not pass as 1
            if not isinstance(up_to, int) or isinstance(up_to, bool):
                raise PriceFileError(f"{here}: up_to must be a whole number of tokens")
            floor = bands[-1].up_to if bands else 0
            if up_to <= floor:
                raise PriceFileError(f"{here}: up_to must be more than "
                                     f"{'the band before' if bands else '0'}")  # fmt: skip
        bands.append(Band(up_to, _rate(band["per_million"], here)))
    return tuple(bands)


def _rate(value: object, where: str) -> Decimal:
    if not isinstance(value, str):
        raise PriceFileError(f'{where}: per_million must be a string like "0.20", not {value!r}')
    try:
        rate = Decimal(value)
    except InvalidOperation:
        rate = None
    if rate is None or not rate.is_finite():
        raise PriceFileError(f"{where}: {value!r} is not a price")
    if rate < 0:
        raise PriceFileError(f"{where}: {value!r} is negative")
    if rate.as_tuple().exponent < -6:
        raise PriceFileError(f"{where}: {value!r} has more than 6 decimal places")
    if rate >= MAX_RATE:
        raise PriceFileError(f"{where}: {value!r} is too large")
    return rate
