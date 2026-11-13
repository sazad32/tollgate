"""Open-loop load generator (ADR 0019): streams started at a fixed rate, in steps, timed per token.

Open loop: each step starts requests on a fixed schedule, whatever happened to the earlier ones. A
closed loop (N users, each waiting for its last answer) would quietly send less when the gateway
slows down, and hide the slowdown ("coordinated omission"). The rate for a step comes from the
concurrency wanted: with streams lasting T seconds, R starts a second keep about R x T open
(Little's law).

What each stream records: time to first token, every gap between tokens arriving, and its whole
duration. Each goes into a histogram with fixed buckets (1 ms apart under 100 ms), so millions of
gaps take no memory and several client pods merge into one result. The client also records how
late it was itself (late starts, its own event-loop lag), so a saturated client can't pass for a
slow gateway.

.NET: like an NBomber scenario with an inject-per-second load simulation, histograms included.
"""

import asyncio
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field

import httpx

CONTENT = b'"content"'
LATE_S = 0.010  # a start this much behind schedule counts as late: the client is falling behind


class LatencyHistogram:
    """Seconds, in buckets 1 ms wide up to 100 ms, 10 ms to 1 s, 100 ms to 10 s, 1 s to 120 s,
    then one overflow bucket. A percentile is the upper edge of the bucket it falls in."""

    EDGES = (
        [i / 1000 for i in range(1, 101)]
        + [i / 100 for i in range(11, 101)]
        + [i / 10 for i in range(11, 101)]
        + [float(i) for i in range(11, 121)]
    )

    def __init__(self) -> None:
        self.counts: dict[int, int] = defaultdict(int)
        self.count = 0
        self.sum_s = 0.0
        self.max_s = 0.0

    @classmethod
    def _index(cls, seconds: float) -> int:
        ms = seconds * 1000
        if ms <= 100:
            return max(0, math.ceil(ms - 1e-9) - 1)
        if ms <= 1000:
            return 100 + math.ceil(ms / 10 - 1e-9) - 11
        if ms <= 10_000:
            return 190 + math.ceil(ms / 100 - 1e-9) - 11
        if ms <= 120_000:
            return 280 + math.ceil(ms / 1000 - 1e-9) - 11
        return len(cls.EDGES)  # overflow

    def add(self, seconds: float) -> None:
        self.counts[self._index(seconds)] += 1
        self.count += 1
        self.sum_s += seconds
        self.max_s = max(self.max_s, seconds)

    def merge(self, other: "LatencyHistogram") -> None:
        for index, n in other.counts.items():
            self.counts[index] += n
        self.count += other.count
        self.sum_s += other.sum_s
        self.max_s = max(self.max_s, other.max_s)

    def percentile(self, p: float) -> float | None:
        if not self.count:
            return None
        rank = max(1, math.ceil(p / 100 * self.count))
        seen = 0
        for index in sorted(self.counts):
            seen += self.counts[index]
            if seen >= rank:
                return self.EDGES[index] if index < len(self.EDGES) else self.max_s
        return self.max_s

    def to_dict(self) -> dict:
        return {"counts": {str(k): v for k, v in self.counts.items()}, "count": self.count,
                "sum": self.sum_s, "max": self.max_s}  # fmt: skip

    @classmethod
    def from_dict(cls, d: dict) -> "LatencyHistogram":
        h = cls()
        h.counts.update({int(k): v for k, v in d["counts"].items()})
        h.count, h.sum_s, h.max_s = d["count"], d["sum"], d["max"]
        return h


@dataclass(frozen=True)
class StepPlan:
    index: int
    concurrency: int  # streams the step aims to keep open, across all client pods
    rate_per_s: float  # starts a second, for one pod
    start_offset_s: float  # from the run's start
    duration_s: float


def plan_steps(concurrencies: list[int], *, step_s: float, tokens: int, tokens_per_s: float,
               first_token_s: float, pods: int) -> list[StepPlan]:  # fmt: skip
    stream_s = first_token_s + (tokens - 1) / tokens_per_s
    return [StepPlan(i, c, c / stream_s / pods, i * step_s, step_s)
            for i, c in enumerate(concurrencies)]  # fmt: skip


@dataclass
class StepResult:
    step: StepPlan
    sent: int = 0
    ok: int = 0
    errors: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    late_starts: int = 0
    in_flight: int = 0
    max_in_flight: int = 0
    client_lag_max_s: float = 0.0
    ttft: LatencyHistogram = field(default_factory=LatencyHistogram)
    gap: LatencyHistogram = field(default_factory=LatencyHistogram)
    duration: LatencyHistogram = field(default_factory=LatencyHistogram)

    def to_record(self, label: str, pod: int) -> dict:
        return {"type": "load_step", "label": label, "step": self.step.index, "pod": pod,
                "concurrency": self.step.concurrency, "rate_per_s": self.step.rate_per_s,
                "duration_s": self.step.duration_s, "sent": self.sent, "ok": self.ok,
                "errors": dict(self.errors), "late_starts": self.late_starts,
                "max_in_flight": self.max_in_flight, "client_lag_max_s": self.client_lag_max_s,
                "ttft": self.ttft.to_dict(), "gap": self.gap.to_dict(),
                "duration": self.duration.to_dict()}  # fmt: skip


def request_body(model: str, tokens: int, index: int) -> dict:
    return {
        "model": model,
        "max_tokens": tokens,
        "ignore_eos": True,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [
            {
                "role": "user",
                "content": f"Load test request {index}. Tell a long "
                f"story about prepaid mobile plans.",
            }
        ],
    }


async def one_stream(client: httpx.AsyncClient, url: str, headers: dict, body: dict,
                     result: StepResult) -> None:  # fmt: skip
    """One streamed request. Never raises: a failure is counted by kind."""
    result.in_flight += 1
    result.max_in_flight = max(result.max_in_flight, result.in_flight)
    start = time.perf_counter()
    try:
        async with client.stream("POST", url, json=body, headers=headers) as response:
            if response.status_code != 200:
                await response.aread()
                result.errors[f"HTTP {response.status_code}"] += 1
                return
            last = None
            async for chunk in response.aiter_raw():
                if CONTENT not in chunk:
                    continue
                now = time.perf_counter()
                if last is None:
                    result.ttft.add(now - start)
                else:
                    result.gap.add(now - last)
                last = now
        result.duration.add(time.perf_counter() - start)
        result.ok += 1
    except (httpx.HTTPError, OSError) as e:
        result.errors[type(e).__name__] += 1
    finally:
        result.in_flight -= 1


async def _watch_lag(result: StepResult, stop: asyncio.Event, interval_s: float = 0.1) -> None:
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        start = loop.time()
        await asyncio.sleep(interval_s)
        result.client_lag_max_s = max(result.client_lag_max_s,
                                      loop.time() - start - interval_s)  # fmt: skip


async def send_step(clients: list[httpx.AsyncClient], base_url: str, step: StepPlan, *, tokens: int,
                    api_key: str | None, model: str = "mock-model",
                    ) -> tuple[StepResult, list[asyncio.Task]]:  # fmt: skip
    """Start the step's requests on schedule; return at once when the step's time is up, with the
    streams still running (the next step starts while they finish)."""
    url = base_url.rstrip("/") + "/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    result = StepResult(step)
    loop = asyncio.get_running_loop()
    begin, end = loop.time(), loop.time() + step.duration_s
    stop = asyncio.Event()
    watcher = asyncio.create_task(_watch_lag(result, stop))
    tasks = []
    for i in range(math.ceil(step.rate_per_s * step.duration_s)):
        due = begin + i / step.rate_per_s
        if due >= end:
            break
        delay = due - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        elif delay < -LATE_S:
            result.late_starts += 1
        body = request_body(model, tokens, step.index * 1_000_000 + i)
        client = clients[i % len(clients)]
        tasks.append(asyncio.create_task(one_stream(client, url, headers, body, result)))
        result.sent += 1
    stop.set()
    tasks.append(watcher)
    return result, tasks


CLIENT_POOLS = 16


def make_clients(transport: httpx.AsyncBaseTransport | None = None) -> list[httpx.AsyncClient]:
    """Sixteen clients, used in turn. httpcore's pool does work that grows with the square of its
    size (ADR 0020), so one pool of thousands would make this client the bottleneck."""
    # No pool limit to speak of: the gateway, not the client's pool, must be what holds requests up.
    limits = httpx.Limits(max_connections=2_000, max_keepalive_connections=2_000)
    timeout = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=60.0)
    tls = httpx.create_ssl_context()  # once: each client would load the CA bundle, 0.3 s apiece
    return [httpx.AsyncClient(timeout=timeout, limits=limits, transport=transport, verify=tls)
            for _ in range(CLIENT_POOLS)]  # fmt: skip


async def _close(clients: list[httpx.AsyncClient]) -> None:
    for client in clients:
        await client.aclose()


async def run_step(base_url: str, step: StepPlan, *, tokens: int, api_key: str | None,
                   transport: httpx.AsyncBaseTransport | None = None) -> StepResult:  # fmt: skip
    """One step on its own, waiting for its last stream."""
    clients = make_clients(transport)
    try:
        result, tasks = await send_step(clients, base_url, step, tokens=tokens, api_key=api_key)
        await asyncio.gather(*tasks)
    finally:
        await _close(clients)
    return result


async def run_load(base_url: str, steps: list[StepPlan], *, tokens: int, api_key: str | None,
                   start_at: float, model: str = "mock-model") -> list[StepResult]:  # fmt: skip
    """Every step, each starting at start_at + its offset (wall clock, so several pods line up)."""
    results, tasks = [], []
    clients = make_clients()
    try:
        for step in steps:
            wait = start_at + step.start_offset_s - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            result, step_tasks = await send_step(clients, base_url, step, tokens=tokens,
                                                 api_key=api_key, model=model)  # fmt: skip
            results.append(result)
            tasks += step_tasks
        await asyncio.gather(*tasks)
    finally:
        await _close(clients)
    return results


def merge_steps(records: list[dict]) -> list[dict]:
    """One row per (label, step) from every pod's records, with percentiles from the merged
    histograms."""
    groups: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for r in records:
        if r.get("type") == "load_step":
            groups[r["label"], r["step"]].append(r)
    rows = []
    for (label, step), group in sorted(groups.items()):
        merged = {name: LatencyHistogram() for name in ("ttft", "gap", "duration")}
        errors: dict[str, int] = defaultdict(int)
        for r in group:
            for name, h in merged.items():
                h.merge(LatencyHistogram.from_dict(r[name]))
            for kind, n in r["errors"].items():
                errors[kind] += n
        row = {"label": label, "step": step, "concurrency": group[0]["concurrency"],
               "pods": len(group), "rate_per_s": sum(r["rate_per_s"] for r in group),
               "sent": sum(r["sent"] for r in group), "ok": sum(r["ok"] for r in group),
               "errors": dict(errors), "late_starts": sum(r["late_starts"] for r in group),
               "max_in_flight": sum(r["max_in_flight"] for r in group),
               "client_lag_max_s": max(r["client_lag_max_s"] for r in group)}  # fmt: skip
        for name, h in merged.items():
            for p in (50, 99, 99.9):
                row[f"{name}_p{p:g}_s"] = h.percentile(p)
            row[f"{name}_max_s"] = h.max_s if h.count else None
        rows.append(row)
    return rows


def _ms(seconds: float | None) -> str:
    return "–" if seconds is None else f"{seconds * 1000:,.0f}"


def load_table(rows: list[dict]) -> str:
    """Markdown: one line per step. Latencies in ms."""
    out = ["| Run | Streams | Starts/s | OK / sent | Errors | First token p50 / p99 | "
           "Token gap p50 / p99 / p99.9 | Max open | Client late |",
           "| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: |"]  # fmt: skip
    for r in rows:
        errors = ", ".join(f"{k} ×{v}" for k, v in sorted(r["errors"].items())) or "–"
        late = f"{r['late_starts']} starts, lag {_ms(r['client_lag_max_s'])} ms"
        out.append(f"| {r['label']} | {r['concurrency']:,} | {r['rate_per_s']:.1f} | "
                   f"{r['ok']:,} / {r['sent']:,} | {errors} | "
                   f"{_ms(r['ttft_p50_s'])} / {_ms(r['ttft_p99_s'])} | "
                   f"{_ms(r['gap_p50_s'])} / {_ms(r['gap_p99_s'])} / {_ms(r['gap_p99.9_s'])} | "
                   f"{r['max_in_flight']:,} | {late} |")  # fmt: skip
    return "\n".join(out)
