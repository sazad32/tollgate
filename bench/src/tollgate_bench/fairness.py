"""Two tenants, one set of GPUs: does a burst from one make the other wait? (ADR 0014)

One tenant (the "flood") sends a burst of requests all at once. The other (the "trickle") sends
one request every few seconds while the burst is being served. The number to watch is the
trickle's time to first token: with fair queuing it waits for about one slot to free up; first
come, first served makes it wait behind the whole burst.

.NET: like starting N tasks at once for one tenant while a PeriodicTimer sends the other's.
"""

import asyncio
import time

import httpx

from tollgate_bench.client import REQUEST_TIMEOUT, RequestResult, one_request
from tollgate_bench.report import fmt_seconds
from tollgate_bench.stats import percentile
from tollgate_bench.workload import MAX_TOKENS


async def run_fairness(
    base_url: str,
    model: str,
    flood_key: str,
    trickle_key: str,
    *,
    run_id: str,
    flood: int = 12,
    trickle: int = 5,
    trickle_start_s: float = 2.0,
    trickle_every_s: float = 5.0,
    max_tokens: int = MAX_TOKENS,
    trickle_url: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[tuple[str, RequestResult]]:
    """Run the burst and the trickle; return (role, result) for every request.

    trickle_url: send the trickle through another gateway replica than the burst, to show the
    two replicas share one queue."""
    urls = {
        "flood": base_url.rstrip("/") + "/v1/chat/completions",
        "trickle": (trickle_url or base_url).rstrip("/") + "/v1/chat/completions",
    }
    limits = httpx.Limits(max_connections=flood + trickle)

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT, limits=limits, transport=transport
    ) as client:

        async def send(role: str, key: str, index: int) -> tuple[str, RequestResult]:
            headers = {"Authorization": f"Bearer {key}"}
            request_id = f"{run_id}-{role}-{index:02d}"
            result = await one_request(
                client, urls[role], headers, model, request_id, flood, max_tokens
            )
            return role, result

        tasks = [asyncio.create_task(send("flood", flood_key, i)) for i in range(flood)]
        await asyncio.sleep(trickle_start_s)  # let the burst fill every slot first
        for i in range(trickle):
            tasks.append(asyncio.create_task(send("trickle", trickle_key, i)))
            if i < trickle - 1:
                await asyncio.sleep(trickle_every_s)
        return list(await asyncio.gather(*tasks))


def summarize_fairness(label: str, results: list[tuple[str, RequestResult]]) -> list[dict]:
    """One summary record per role: how many got through, and how long they waited."""
    first_start = min(r.started_at for _, r in results)
    summaries = []
    for role in ("flood", "trickle"):
        mine = [r for rl, r in results if rl == role]
        ok = [r for r in mine if r.error is None and r.ttft_s is not None]
        ttfts = [r.ttft_s for r in ok if r.ttft_s is not None]
        summaries.append(
            {
                "type": "fairness",
                "label": label,
                "tenant": role,
                "requests": len(mine),
                "ok": len(ok),
                "rejected": sum(1 for r in mine if r.error and r.error.startswith("HTTP 429")),
                "ttft_p50_s": percentile(ttfts, 50),
                "ttft_max_s": max(ttfts) if ttfts else None,
                # When the last of this role's requests finished, counted from the burst's start.
                "done_after_s": max(r.started_at + r.latency_s for r in mine) - first_start
                if mine
                else None,
            }
        )
    return summaries


def fairness_table(records: list[dict]) -> str:
    rows = [r for r in records if r.get("type") == "fairness"]
    if not rows:
        return ""
    out = [
        "| Run | Tenant | OK / sent | 429s | First token p50 | First token worst | All done |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in rows:
        out.append(
            f"| {r['label']} | {r['tenant']} | {r['ok']} / {r['requests']} | {r['rejected']} "
            f"| {fmt_seconds(r['ttft_p50_s'])} | {fmt_seconds(r['ttft_max_s'])} "
            f"| {fmt_seconds(r['done_after_s'])} |"
        )
    return "\n".join(out) + "\n"


def now_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S")
