"""The load generator against a fake OpenAI-compatible server (httpx.MockTransport, no network)."""

import asyncio
import json

import httpx
import pytest
from tollgate_bench.client import run_level

TOKENS = 4


def sse_body(tokens: int = TOKENS) -> bytes:
    """A streamed chat completion: a role chunk, one chunk per token, usage, then [DONE]."""
    chunks = [{"choices": [{"delta": {"role": "assistant"}}]}]
    chunks += [{"choices": [{"delta": {"content": f"t{i} "}}]} for i in range(tokens)]
    chunks.append({"choices": [], "usage": {"prompt_tokens": 256, "completion_tokens": tokens}})
    lines = [f"data: {json.dumps(c)}\n\n" for c in chunks] + ["data: [DONE]\n\n"]
    return "".join(lines).encode()


class FakeServer:
    """Answers every request after a short delay and records how many were in flight at once."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.in_flight = 0
        self.max_in_flight = 0
        self.bodies: list[dict] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0.01)
        self.in_flight -= 1
        if self.status != 200:
            return httpx.Response(self.status, text="busy")
        return httpx.Response(200, content=sse_body())


@pytest.mark.asyncio
async def test_runs_every_request_with_exactly_n_in_flight() -> None:
    server = FakeServer()

    run = await run_level(
        "http://fake", "m", concurrency=3, total_requests=7, run_id="t",
        transport=httpx.MockTransport(server.handle),
    )  # fmt: skip

    assert len(run.results) == 7
    assert server.max_in_flight == 3
    assert run.window_end >= run.window_start


@pytest.mark.asyncio
async def test_each_request_is_timed_and_uses_the_servers_token_counts() -> None:
    server = FakeServer()

    run = await run_level("http://fake", "m", 1, 1, run_id="t",
                          transport=httpx.MockTransport(server.handle))  # fmt: skip
    r = run.results[0]

    assert r.error is None
    assert r.ttft_s is not None
    assert 0 < r.ttft_s <= r.latency_s
    assert r.output_tokens == TOKENS
    assert r.prompt_tokens == 256


@pytest.mark.asyncio
async def test_request_ids_are_unique_so_prompts_cant_be_cached() -> None:
    server = FakeServer()

    await run_level("http://fake", "m", 2, 4, run_id="t",
                    transport=httpx.MockTransport(server.handle))  # fmt: skip
    prompts = [b["messages"][0]["content"] for b in server.bodies]

    assert len(set(prompts)) == 4
    assert all(b["ignore_eos"] and b["stream"] for b in server.bodies)


@pytest.mark.asyncio
async def test_http_errors_are_recorded_not_raised() -> None:
    server = FakeServer(status=503)

    run = await run_level("http://fake", "m", 2, 2, run_id="t",
                          transport=httpx.MockTransport(server.handle))  # fmt: skip

    assert [r.error for r in run.results] == ["HTTP 503: busy", "HTTP 503: busy"]
    assert all(r.ttft_s is None for r in run.results)


@pytest.mark.asyncio
async def test_api_key_is_sent_as_a_bearer_token() -> None:
    seen: list[str | None] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, content=sse_body())

    await run_level("http://fake", "m", 1, 1, run_id="t", api_key="k",
                    transport=httpx.MockTransport(handle))  # fmt: skip

    assert seen == ["Bearer k"]
