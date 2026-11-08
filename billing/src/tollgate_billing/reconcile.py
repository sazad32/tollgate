"""Reconciliation (ADR 0018): the ledger against the model servers' own token counters.

For a window (normally yesterday, UTC) and each local backend, compare:
- the output tokens the ledger measured for requests that finished on that backend in the window;
- the tokens the server itself says it generated: its Prometheus counter, read from raw samples.

Only output tokens are compared. llama-server's prompt counter skips prompt tokens it had cached
(measured: 1 of 75 for a repeated prompt), so it can't check the ledger's input tokens.
The hosted model isn't reconciled: its ledger rows already carry Anthropic's own counts.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

import httpx
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from tollgate_billing.db import LedgerRow, ReconciliationRow

# The counter of generated tokens, by kind of server.
OUTPUT_COUNTERS = {
    "llama": "llamacpp:tokens_predicted_total",
    "vllm": "vllm:generation_tokens_total",
}
LOOKBACK_S = 300  # also fetch this much before the window, for the sample to count from

Sample = tuple[float, float]  # (unix time, counter value)


class ReconcileError(Exception):
    pass


class ReconcileBackend(BaseModel):
    """A local backend to reconcile. `backend` is the gateway's name for it (the ledger's backend
    column); `series` is its `backend` label in Prometheus, when that differs."""

    backend: str
    kind: Literal["llama", "vllm"]
    series: str | None = None

    @property
    def series_label(self) -> str:
        return self.series or self.backend


@dataclass(frozen=True)
class CounterWindow:
    increase: int
    since: float  # time of the sample counted from
    until: float  # time of the last sample counted


def counter_window(series: list[list[Sample]], start: float, end: float) -> CounterWindow | None:
    """How much a counter grew over (start, end], from raw samples. No extrapolation, unlike
    PromQL's increase(): the count runs from the last sample at or before `start` to the last at or
    before `end`, so the ledger can be summed over exactly that span.

    - A counter that goes down was restarted, and counts again from zero.
    - A series that first appears inside the window (a new pod) counts from its first sample: a new
      server is scraped before the gateway routes to it, so nothing is lost.
    - Several series (a pod replaced, with a new address) all count.
    Returns None if no series has a sample inside the window.
    """
    total, since, until = 0.0, None, None
    for samples in series:
        samples = sorted(samples)
        inside = [s for s in samples if start < s[0] <= end]
        if not inside:
            continue
        before = [s for s in samples if s[0] <= start]
        first = before[-1] if before else inside[0]
        previous = first[1]
        for _, value in inside:
            total += value - previous if value >= previous else value
            previous = value
        since = first[0] if since is None else min(since, first[0])
        until = inside[-1][0] if until is None else max(until, inside[-1][0])
    if since is None:
        return None
    return CounterWindow(round(total), since, until)


async def fetch_series(http: httpx.AsyncClient, prometheus_url: str, backend: ReconcileBackend,
                       start: float, end: float) -> list[list[Sample]]:  # fmt: skip
    """Raw samples of the backend's output counter over the window (plus LOOKBACK_S before it),
    one list per series."""
    metric = OUTPUT_COUNTERS[backend.kind]
    span = round(end - start) + LOOKBACK_S
    query = f'{metric}{{backend="{backend.series_label}"}}[{span}s]'
    try:
        response = await http.get(f"{prometheus_url.rstrip('/')}/api/v1/query",
                                  params={"query": query, "time": f"{end:.3f}"})  # fmt: skip
        body = response.json()
    except (httpx.HTTPError, ValueError) as e:
        raise ReconcileError(f"Prometheus: {e}") from None
    if response.status_code != 200 or body.get("status") != "success":
        raise ReconcileError(f"Prometheus: {body.get('error', response.status_code)}")
    return [[(float(t), float(v)) for t, v in r["values"]] for r in body["data"]["result"]]


@dataclass(frozen=True)
class Tolerance:
    """How far the server may be from the ledger and still pass. By default only the estimated
    tokens may be off, by half: a hang-up's estimate misses the tokens already in flight when the
    gateway cancelled (measured: estimated 19, generated 21)."""

    tokens: int = 0
    ratio: float = 0.0  # of the ledger's tokens
    estimate_share: float = 0.5  # of the ledger's estimated tokens


@dataclass(frozen=True)
class Check:
    backend: str
    reported: int  # ledger output tokens the model reported
    estimated: int  # ledger output tokens the gateway estimated
    server: int | None  # the server's counter; None: no samples
    allowed: int
    status: str  # exact | within tolerance | gap | no data | idle
    since: datetime | None = None  # the span compared
    until: datetime | None = None

    @property
    def ledger(self) -> int:
        return self.reported + self.estimated

    @property
    def gap(self) -> int | None:
        return None if self.server is None else self.server - self.ledger

    @property
    def failed(self) -> bool:
        return self.status == "gap"


def judge(backend: str, *, reported: int, estimated: int, server: int | None,
          tolerance: Tolerance, since: datetime | None = None,
          until: datetime | None = None) -> Check:  # fmt: skip
    ledger = reported + estimated
    allowed = int(
        tolerance.tokens + tolerance.ratio * ledger + tolerance.estimate_share * estimated
    )
    if server is None:
        status = "idle" if ledger == 0 else "no data"
    elif server == ledger:
        status = "exact"
    elif abs(server - ledger) <= allowed:
        status = "within tolerance"
    else:
        status = "gap"
    return Check(backend, reported, estimated, server, allowed, status, since, until)


async def ledger_output(engine: AsyncEngine, backend: str, since: datetime,
                        until: datetime) -> tuple[int, int]:  # fmt: skip
    """Measured output tokens of the backend's requests that finished in (since, until]:
    (reported, estimated)."""
    query = (
        select(LedgerRow.token_source, func.sum(LedgerRow.output_tokens))
        .where(
            LedgerRow.backend == backend,
            LedgerRow.finished_at > since,
            LedgerRow.finished_at <= until,
        )  # fmt: skip
        .group_by(LedgerRow.token_source)
    )
    async with engine.connect() as conn:
        sums = dict((await conn.execute(query)).all())
    return int(sums.get("reported") or 0), int(sums.get("estimated") or 0)


async def reconcile(engine: AsyncEngine, http: httpx.AsyncClient, prometheus_url: str,
                    backends: list[ReconcileBackend], start: datetime, end: datetime,
                    tolerance: Tolerance) -> list[Check]:  # fmt: skip
    """One check per backend over (start, end]. Where the server has samples, the ledger is summed
    over exactly the span the counter covers, so the two count the same requests."""
    checks = []
    for backend in backends:
        series = await fetch_series(http, prometheus_url, backend, start.timestamp(),
                                    end.timestamp())  # fmt: skip
        window = counter_window(series, start.timestamp(), end.timestamp())
        if window is None:
            since, until, server = start, end, None
        else:
            since = datetime.fromtimestamp(window.since, start.tzinfo)
            until = datetime.fromtimestamp(window.until, start.tzinfo)
            server = window.increase
        reported, estimated = await ledger_output(engine, backend.backend, since, until)
        checks.append(
            judge(
                backend.backend,
                reported=reported,
                estimated=estimated,
                server=server,
                tolerance=tolerance,
                since=since,
                until=until,
            )
        )
    return checks


async def store(engine: AsyncEngine, day: date, checks: list[Check]) -> None:
    """Keep a day's results. Running the day again replaces them: this is a measurement."""
    rows = [
        {"day": day, "backend": c.backend, "meter": "output", "ledger_reported": c.reported,
         "ledger_estimated": c.estimated, "server": c.server, "allowed": c.allowed,
         "status": c.status, "since": c.since, "until": c.until}
        for c in checks
    ]  # fmt: skip
    if not rows:
        return
    statement = insert(ReconciliationRow).values(rows)
    replace = {k: statement.excluded[k] for k in rows[0] if k not in ("day", "backend", "meter")}
    async with engine.begin() as conn:
        await conn.execute(
            statement.on_conflict_do_update(
                index_elements=["day", "backend", "meter"],
                set_={**replace, "checked_at": func.now()},
            )
        )


def report(checks: list[Check]) -> list[str]:
    out = [f"  {'backend':<16}{'ledger':>9}{'(estimated)':>13}{'server':>9}{'gap':>7}"
           f"{'allowed':>9}  status"]  # fmt: skip
    for c in checks:
        server = "-" if c.server is None else f"{c.server:,}"
        gap = "-" if c.gap is None else f"{c.gap:+,}"
        out.append(f"  {c.backend:<16}{c.ledger:>9,}{c.estimated:>13,}{server:>9}{gap:>7}"
                   f"{c.allowed:>9,}  {c.status}")  # fmt: skip
    return out
