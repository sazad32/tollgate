"""Turn one level's request results into the numbers in the results table."""

from dataclasses import dataclass

from tollgate_bench.client import LevelRun


def percentile(values: list[float], p: float) -> float | None:
    """The p-th percentile (0-100) with linear interpolation between ranks; None for no values.

    Same method as numpy's default and Excel's PERCENTILE.INC.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * p / 100
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


@dataclass(frozen=True)
class LevelSummary:
    backend: str
    concurrency: int
    requests: int
    errors: int
    duration_s: float
    requests_per_s: float
    output_tokens_per_s: float  # all users together
    ttft_p50_s: float | None
    ttft_p95_s: float | None
    tpot_p50_s: float | None  # time per output token after the first, per request
    tpot_p95_s: float | None
    mean_prompt_tokens: float | None
    mean_output_tokens: float | None


def summarize(backend: str, run: LevelRun) -> LevelSummary:
    ok = [r for r in run.results if r.error is None and r.ttft_s is not None]
    duration = max(run.window_end - run.window_start, 1e-9)
    output_tokens = [r.output_tokens for r in ok if r.output_tokens is not None]
    prompt_tokens = [r.prompt_tokens for r in ok if r.prompt_tokens is not None]
    # Time per output token: the generation phase only (after the first token), per token.
    tpot = [
        (r.latency_s - r.ttft_s) / (r.output_tokens - 1)
        for r in ok
        if r.output_tokens is not None and r.output_tokens > 1
    ]
    ttft = [r.ttft_s for r in ok]
    return LevelSummary(
        backend=backend,
        concurrency=run.concurrency,
        requests=len(run.results),
        errors=len(run.results) - len(ok),
        duration_s=duration,
        requests_per_s=len(ok) / duration,
        output_tokens_per_s=sum(output_tokens) / duration,
        ttft_p50_s=percentile(ttft, 50),
        ttft_p95_s=percentile(ttft, 95),
        tpot_p50_s=percentile(tpot, 50),
        tpot_p95_s=percentile(tpot, 95),
        mean_prompt_tokens=sum(prompt_tokens) / len(prompt_tokens) if prompt_tokens else None,
        mean_output_tokens=sum(output_tokens) / len(output_tokens) if output_tokens else None,
    )
