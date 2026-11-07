"""Closed-loop load generator: N users, each sending its next request as soon as the last one ends.

.NET: like Parallel.ForEachAsync with MaxDegreeOfParallelism = N over a fixed list of requests, each
awaiting a streamed HttpClient response.
Gotcha: everything runs on one asyncio thread. A blocking call anywhere (time.sleep, sync I/O) would
stall every user at once and skew the timings, so only async I/O is allowed here.
"""

import asyncio
import time
from dataclasses import dataclass, field

import httpx

from tollgate_bench.sse import has_content, parse_line
from tollgate_bench.workload import MAX_TOKENS, chat_body

# A queued request on a busy llama-server can wait minutes before its first byte, so the read
# timeout (time allowed between two chunks) must cover the whole queue.
REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=900.0, write=10.0, pool=900.0)


@dataclass(frozen=True)
class RequestResult:
    """One request as the client saw it. Times are in seconds."""

    concurrency: int
    request_id: str
    started_at: float  # wall clock (epoch seconds), to line requests up with Prometheus
    ttft_s: float | None  # time to first generated token; None if none arrived
    latency_s: float  # request start to end of stream
    prompt_tokens: int | None  # as counted by the server
    output_tokens: int | None
    error: str | None = None


@dataclass
class LevelRun:
    """All requests at one concurrency level, and the window they ran in."""

    concurrency: int
    window_start: float  # wall clock
    window_end: float
    results: list[RequestResult] = field(default_factory=list)


async def one_request(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    model: str,
    request_id: str,
    concurrency: int,
    max_tokens: int = MAX_TOKENS,
) -> RequestResult:
    """Send one streamed chat completion and time it. Never raises: failures become `error`."""
    started_at = time.time()
    start = time.perf_counter()
    ttft: float | None = None
    usage: dict = {}
    error: str | None = None
    try:
        body = chat_body(model, request_id, max_tokens)
        async with client.stream("POST", url, json=body, headers=headers) as response:
            if response.status_code != 200:
                text = (await response.aread()).decode(errors="replace")[:200]
                error = f"HTTP {response.status_code}: {text}"
            else:
                async for line in response.aiter_lines():
                    chunk = parse_line(line)
                    if chunk is None:
                        continue
                    if ttft is None and has_content(chunk):
                        ttft = time.perf_counter() - start
                    if chunk.get("usage"):
                        usage = chunk["usage"]
    except (httpx.HTTPError, ValueError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    return RequestResult(
        concurrency=concurrency,
        request_id=request_id,
        started_at=started_at,
        ttft_s=ttft,
        latency_s=time.perf_counter() - start,
        prompt_tokens=usage.get("prompt_tokens"),
        output_tokens=usage.get("completion_tokens"),
        error=error,
    )


async def run_level(
    base_url: str,
    model: str,
    concurrency: int,
    total_requests: int,
    *,
    run_id: str,
    api_key: str | None = None,
    max_tokens: int = MAX_TOKENS,
    transport: httpx.AsyncBaseTransport | None = None,
) -> LevelRun:
    """Run `total_requests` requests, `concurrency` at a time, until the list runs out."""
    url = base_url.rstrip("/") + "/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    run = LevelRun(concurrency=concurrency, window_start=time.time(), window_end=0.0)
    next_index = 0

    async def user(client: httpx.AsyncClient) -> None:
        nonlocal next_index
        # Safe without a lock: asyncio switches tasks only at an `await`, and there is none
        # between reading next_index and incrementing it.
        while next_index < total_requests:
            index = next_index
            next_index += 1
            request_id = f"{run_id}-c{concurrency}-{index:04d}"
            result = await one_request(
                client, url, headers, model, request_id, concurrency, max_tokens
            )
            run.results.append(result)

    # TaskGroup is Task.WhenAll with cleanup: if one task fails, the others are cancelled.
    # Leaving the `async with` block waits for every user to finish.
    async with (
        httpx.AsyncClient(timeout=REQUEST_TIMEOUT, limits=limits, transport=transport) as client,
        asyncio.TaskGroup() as group,
    ):
        for _ in range(concurrency):
            group.create_task(user(client))
    run.window_end = time.time()
    return run
