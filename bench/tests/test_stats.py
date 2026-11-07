import pytest
from tollgate_bench.client import LevelRun, RequestResult
from tollgate_bench.stats import percentile, summarize


def result(ttft: float | None, latency: float, out: int | None = 11, error: str | None = None):
    return RequestResult(
        concurrency=2,
        request_id="r",
        started_at=0.0,
        ttft_s=ttft,
        latency_s=latency,
        prompt_tokens=256,
        output_tokens=out,
        error=error,
    )


def test_percentile_interpolates_between_ranks() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert percentile(values, 50) == 3.0
    assert percentile(values, 95) == pytest.approx(4.8)
    assert percentile(values, 0) == 1.0
    assert percentile(values, 100) == 5.0


def test_percentile_of_nothing_is_none() -> None:
    assert percentile([], 50) is None


def test_summary_counts_tokens_over_the_whole_window() -> None:
    run = LevelRun(concurrency=2, window_start=100.0, window_end=110.0)
    run.results += [result(0.5, 2.5), result(1.5, 3.5)]

    s = summarize("test", run)

    assert s.requests == 2
    assert s.errors == 0
    assert s.output_tokens_per_s == pytest.approx(2.2)  # 22 tokens in 10 s
    assert s.requests_per_s == pytest.approx(0.2)
    # Time per output token: (latency - ttft) / (tokens - 1) = 2.0 / 10 for both requests.
    assert s.tpot_p50_s == pytest.approx(0.2)
    assert s.ttft_p50_s == pytest.approx(1.0)


def test_failed_requests_count_as_errors_and_are_left_out_of_timings() -> None:
    run = LevelRun(concurrency=2, window_start=0.0, window_end=10.0)
    run.results += [result(0.5, 2.5), result(None, 900.0, out=None, error="ReadTimeout: ...")]

    s = summarize("test", run)

    assert s.requests == 2
    assert s.errors == 1
    assert s.ttft_p95_s == pytest.approx(0.5)
    assert s.output_tokens_per_s == pytest.approx(1.1)


def test_a_request_with_no_generated_text_is_an_error() -> None:
    run = LevelRun(concurrency=1, window_start=0.0, window_end=1.0)
    run.results.append(result(None, 1.0, out=0))

    assert summarize("test", run).errors == 1
