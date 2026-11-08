"""Reconciliation (ADR 0018): a model server's token counter, read exactly from raw samples, and the
verdict on how far the ledger is from it."""

import httpx
import pytest
from tollgate_billing.reconcile import (
    CounterWindow,
    ReconcileBackend,
    ReconcileError,
    Tolerance,
    counter_window,
    fetch_series,
    judge,
)

START, END = 1_000_000.0, 1_086_400.0  # one day


# --- the counter ----------------------------------------------------------------------------------


def test_the_increase_counts_from_the_last_sample_before_the_window() -> None:
    series = [[(START - 10, 100.0), (START + 5, 110.0), (END - 3, 150.0)]]

    assert counter_window(series, START, END) == CounterWindow(50, START - 10, END - 3)


def test_samples_outside_the_window_are_ignored() -> None:
    series = [[(START - 100, 50.0), (START - 10, 100.0), (END - 1, 130.0), (END + 5, 200.0)]]

    assert counter_window(series, START, END).increase == 30


def test_a_restarted_server_counts_again_from_zero() -> None:
    # 100 -> 120 (+20), restart, 5 (+5 since the restart), 30 (+25)
    series = [[(START - 10, 100.0), (START + 5, 120.0), (START + 20, 5.0), (END - 1, 30.0)]]

    assert counter_window(series, START, END).increase == 50


def test_a_server_that_first_appears_in_the_window_counts_from_its_first_sample() -> None:
    # A new pod's first scrape comes before the gateway routes to it; the counter is 0 there.
    series = [[(START + 100, 0.0), (START + 200, 40.0)]]

    assert counter_window(series, START, END) == CounterWindow(40, START + 100, START + 200)


def test_a_replaced_pod_is_a_second_series_and_both_count() -> None:
    old = [(START - 10, 100.0), (START + 50, 140.0)]
    new = [(START + 60, 0.0), (END - 5, 25.0)]

    assert counter_window([old, new], START, END) == CounterWindow(65, START - 10, END - 5)


def test_no_samples_in_the_window_is_no_data() -> None:
    assert counter_window([], START, END) is None
    assert counter_window([[(START - 10, 100.0)]], START, END) is None  # gone before the window


# --- the verdict ----------------------------------------------------------------------------------

STRICT = Tolerance(tokens=0, ratio=0.0, estimate_share=0.5)


def test_an_exact_match() -> None:
    check = judge("llama-server-0", reported=120, estimated=0, server=120, tolerance=STRICT)

    assert (check.status, check.gap, check.failed) == ("exact", 0, False)


def test_estimated_tokens_may_be_off_by_their_share() -> None:
    # Measured: a hang-up estimated at 19 output tokens; the server had generated 21.
    check = judge("llama-server-0", reported=120, estimated=19, server=141, tolerance=STRICT)

    assert (check.status, check.gap, check.allowed, check.failed) == ("within tolerance", 2, 9,
                                                                       False)  # fmt: skip


def test_tokens_the_ledger_never_saw_are_a_gap() -> None:
    # A lost event, or a request that skipped the gateway: the server generated 40 more.
    check = judge("llama-server-0", reported=120, estimated=0, server=160, tolerance=STRICT)

    assert (check.status, check.gap, check.failed) == ("gap", 40, True)


def test_more_in_the_ledger_than_the_server_generated_is_a_gap() -> None:
    check = judge("llama-server-0", reported=160, estimated=0, server=120, tolerance=STRICT)

    assert (check.status, check.gap, check.failed) == ("gap", -40, True)


def test_a_fixed_and_proportional_tolerance_can_be_allowed() -> None:
    loose = Tolerance(tokens=10, ratio=0.1, estimate_share=0.0)

    assert judge("b", reported=1000, estimated=0, server=1110, tolerance=loose).status == (
        "within tolerance"
    )
    assert judge("b", reported=1000, estimated=0, server=1111, tolerance=loose).status == "gap"


def test_no_server_data_is_reported_not_failed() -> None:
    assert judge("vllm", reported=0, estimated=0, server=None, tolerance=STRICT).status == "idle"
    check = judge("vllm", reported=50, estimated=0, server=None, tolerance=STRICT)

    assert (check.status, check.gap, check.failed) == ("no data", None, False)


# --- Prometheus -----------------------------------------------------------------------------------


def prometheus(result: list[dict], seen: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "matrix", "result": result}}
        )

    return httpx.MockTransport(handle)


@pytest.mark.asyncio
async def test_fetch_series_asks_for_raw_samples_of_one_backend() -> None:
    seen: list[httpx.Request] = []
    result = [{"metric": {"backend": "llama-server-0", "instance": "a"},
               "values": [[START - 10, "100"], [END - 3, "150"]]},
              {"metric": {"backend": "llama-server-0", "instance": "b"},
               "values": [[START + 60, "0"], [END - 5, "25"]]}]  # fmt: skip
    backend = ReconcileBackend(backend="llama-server-0", kind="llama")

    async with httpx.AsyncClient(transport=prometheus(result, seen)) as http:
        series = await fetch_series(http, "http://prom:9090", backend, START, END)

    assert series == [[(START - 10, 100.0), (END - 3, 150.0)], [(START + 60, 0.0), (END - 5, 25.0)]]
    [request] = seen
    assert request.url.path == "/api/v1/query"
    assert request.url.params["query"] == (
        'llamacpp:tokens_predicted_total{backend="llama-server-0"}[86700s]'  # the day + 5 min
    )
    assert float(request.url.params["time"]) == END


@pytest.mark.asyncio
async def test_a_backend_can_have_another_name_in_prometheus() -> None:
    seen: list[httpx.Request] = []
    backend = ReconcileBackend(backend="vllm", kind="vllm", series="vllm-workstation")

    async with httpx.AsyncClient(transport=prometheus([], seen)) as http:
        assert await fetch_series(http, "http://prom:9090", backend, START, END) == []

    assert (
        seen[0]
        .url.params["query"]
        .startswith('vllm:generation_tokens_total{backend="vllm-workstation"}[')
    )


@pytest.mark.asyncio
async def test_a_prometheus_error_is_raised_not_read_as_zero() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"status": "error", "error": "bad query"})

    backend = ReconcileBackend(backend="llama-server-0", kind="llama")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(ReconcileError, match="bad query"):
            await fetch_series(http, "http://prom:9090", backend, START, END)


def test_the_report_keeps_large_numbers_apart() -> None:
    from tollgate_billing.reconcile import report

    big = judge("llama-server-0", reported=0, estimated=0, server=157_923, tolerance=STRICT)

    row = report([big])[1].split()
    assert row == ["llama-server-0", "0", "0", "157,923", "+157,923", "0", "gap"]
