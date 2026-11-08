"""The two-tenant fairness demo against a fake gateway (httpx.MockTransport, no network)."""

import asyncio
import json

import httpx
import pytest
from tollgate_bench.client import RequestResult
from tollgate_bench.fairness import fairness_table, run_fairness, summarize_fairness


def sse_body() -> bytes:
    chunks = [
        {"choices": [{"delta": {"content": "hi"}}]},
        {"choices": [], "usage": {"prompt_tokens": 256, "completion_tokens": 1}},
    ]
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks).encode() + b"data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_both_tenants_send_their_requests_with_their_own_keys() -> None:
    seen: list[str] = []
    hosts: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        hosts.append(request.url.host)
        await asyncio.sleep(0.01)
        return httpx.Response(200, content=sse_body())

    results = await run_fairness(
        "http://gateway", "m", "flood-key", "trickle-key", run_id="t", flood=4, trickle=2,
        trickle_start_s=0.01, trickle_every_s=0.01, trickle_url="http://replica-b",
        transport=httpx.MockTransport(handle),
    )  # fmt: skip

    assert sorted(role for role, _ in results) == ["flood"] * 4 + ["trickle"] * 2
    assert seen.count("Bearer flood-key") == 4
    assert seen.count("Bearer trickle-key") == 2
    assert all(r.error is None for _, r in results)
    assert hosts.count("gateway") == 4 and hosts.count("replica-b") == 2


def result(started: float, ttft: float | None, latency: float, error: str | None = None):
    return RequestResult(
        concurrency=4, request_id="r", started_at=started, ttft_s=ttft, latency_s=latency,
        prompt_tokens=256, output_tokens=128, error=error,
    )  # fmt: skip


def test_the_summary_counts_waits_429s_and_when_each_tenant_was_done() -> None:
    results = [
        ("flood", result(100.0, 1.0, 10.0)),
        ("flood", result(100.0, 9.0, 20.0)),
        ("flood", result(100.0, None, 0.1, error="HTTP 429: queue full")),
        ("trickle", result(102.0, 3.0, 12.0)),
    ]

    flood, trickle = summarize_fairness("fair queue on", results)

    assert (flood["ok"], flood["requests"], flood["rejected"]) == (2, 3, 1)
    assert flood["ttft_p50_s"] == pytest.approx(5.0)
    assert flood["ttft_max_s"] == pytest.approx(9.0)
    assert flood["done_after_s"] == pytest.approx(20.0)
    assert trickle["done_after_s"] == pytest.approx(14.0)


def test_the_table_has_a_row_per_run_and_tenant() -> None:
    results = [("flood", result(0.0, 1.0, 2.0)), ("trickle", result(0.0, 0.5, 1.0))]
    records = summarize_fairness("off", results) + summarize_fairness("on", results)

    table = fairness_table(records)

    assert table.count("\n| off |") == 2 and table.count("\n| on |") == 2
    assert "| 1 / 1 | 0 | 1.0 s |" in table
