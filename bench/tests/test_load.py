"""The open-loop load generator (ADR 0019): fixed arrival rates in steps, latency in histograms."""

import httpx
import pytest
from tollgate_bench.load import LatencyHistogram, StepPlan, merge_steps, plan_steps, run_step
from tollgate_bench.mock import MockSettings, create_app

# --- the histogram --------------------------------------------------------------------------------


def test_percentiles_under_100_ms_are_exact_to_the_millisecond() -> None:
    h = LatencyHistogram()
    for ms in range(1, 101):  # 1..100 ms
        h.add(ms / 1000)

    assert h.count == 100
    assert h.percentile(50) == pytest.approx(0.050, abs=0.001)
    assert h.percentile(99) == pytest.approx(0.099, abs=0.001)
    assert h.max_s == pytest.approx(0.100)


def test_long_values_land_in_coarser_buckets() -> None:
    h = LatencyHistogram()
    h.add(0.2345)
    h.add(3.21)
    h.add(500.0)  # beyond the last bucket: kept, as the overflow

    assert h.percentile(1) == pytest.approx(0.24, abs=0.01)
    assert h.percentile(60) == pytest.approx(3.3, abs=0.1)
    assert h.max_s == pytest.approx(500.0)


def test_histograms_from_several_pods_merge_and_survive_json() -> None:
    a, b = LatencyHistogram(), LatencyHistogram()
    for _ in range(90):
        a.add(0.010)
    for _ in range(10):
        b.add(0.500)

    merged = LatencyHistogram.from_dict(a.to_dict())
    merged.merge(LatencyHistogram.from_dict(b.to_dict()))

    assert merged.count == 100
    assert merged.percentile(50) == pytest.approx(0.010, abs=0.001)
    assert merged.percentile(95) == pytest.approx(0.50, abs=0.01)
    assert merged.max_s == pytest.approx(0.5)


def test_an_empty_histogram_has_no_percentiles() -> None:
    assert LatencyHistogram().percentile(99) is None


# --- the plan -------------------------------------------------------------------------------------


def test_each_steps_rate_holds_its_concurrency_and_pods_share_it() -> None:
    # 256 tokens at 20/s after 100 ms: a stream lasts 12.85 s. Little's law: rate = streams / time.
    steps = plan_steps([100, 1000], step_s=60, tokens=256, tokens_per_s=20, first_token_s=0.1,
                       pods=4)  # fmt: skip

    assert steps[0] == StepPlan(index=0, concurrency=100, rate_per_s=pytest.approx(100 / 12.85 / 4),
                                start_offset_s=0, duration_s=60)  # fmt: skip
    assert steps[1].concurrency == 1000
    assert steps[1].rate_per_s == pytest.approx(1000 / 12.85 / 4)
    assert steps[1].start_offset_s == 60


# --- a step, against the mock ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_step_sends_at_its_rate_and_times_every_stream() -> None:
    mock = create_app(MockSettings(tokens_per_s=1000.0, first_token_s=0.01))
    step = StepPlan(index=0, concurrency=5, rate_per_s=50.0, start_offset_s=0, duration_s=0.4)

    result = await run_step("http://mock", step, tokens=8, api_key=None,
                            transport=httpx.ASGITransport(app=mock))  # fmt: skip

    assert 18 <= result.sent <= 21  # 50 a second for 0.4 s
    assert result.ok == result.sent
    assert result.errors == {}
    assert result.ttft.count == result.ok
    assert result.duration.count == result.ok
    assert result.max_in_flight >= 1


@pytest.mark.asyncio
async def test_failures_are_counted_by_kind() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls % 2:
            return httpx.Response(429, json={"error": {"message": "slow down"}})
        raise httpx.ConnectError("refused")

    step = StepPlan(index=0, concurrency=1, rate_per_s=40.0, start_offset_s=0, duration_s=0.25)
    result = await run_step("http://gw", step, tokens=4, api_key="k",
                            transport=httpx.MockTransport(handle))  # fmt: skip

    assert result.ok == 0
    assert set(result.errors) == {"HTTP 429", "ConnectError"}
    assert sum(result.errors.values()) == result.sent


def test_steps_from_several_pods_merge_into_one_row() -> None:
    def record(pod: int, ok: int, ttft_s: float) -> dict:
        h = LatencyHistogram()
        for _ in range(ok):
            h.add(ttft_s)
        return {"type": "load_step", "label": "run", "step": 0, "pod": pod, "concurrency": 100,
                "rate_per_s": 2.0, "duration_s": 60, "sent": ok + 1, "ok": ok,
                "errors": {"HTTP 429": 1}, "late_starts": 0, "max_in_flight": 50,
                "client_lag_max_s": 0.002, "ttft": h.to_dict(), "gap": LatencyHistogram().to_dict(),
                "duration": h.to_dict()}  # fmt: skip

    [row] = merge_steps([record(0, 10, 0.1), record(1, 30, 0.3)])

    assert (row["label"], row["concurrency"], row["sent"], row["ok"]) == ("run", 100, 42, 40)
    assert row["errors"] == {"HTTP 429": 2}
    assert row["rate_per_s"] == pytest.approx(4.0)
    assert row["ttft_p50_s"] == pytest.approx(0.3, abs=0.01)
    assert row["max_in_flight"] == 100


@pytest.mark.asyncio
async def test_streams_are_spread_over_several_small_pools() -> None:
    # One big httpx pool costs CPU with the square of its size (ADR 0020): the client would be
    # the bottleneck long before the gateway.
    from tollgate_bench.load import CLIENT_POOLS, make_clients

    clients = make_clients()
    try:
        assert len(clients) == CLIENT_POOLS == 16
        assert len({id(c) for c in clients}) == 16
    finally:
        for c in clients:
            await c.aclose()
