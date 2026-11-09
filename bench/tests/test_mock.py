"""The fake model server for load tests (ADR 0019): OpenAI-compatible, no GPU, paced tokens."""

import json
import time

import httpx
import pytest
from tollgate_bench.mock import MockSettings, create_app
from tollgate_bench.sse import parse_line

pytestmark = pytest.mark.asyncio

FAST = MockSettings(tokens_per_s=2000.0, first_token_s=0.0)


def body(**extra) -> dict:
    return {
        "model": "mock-model",
        "max_tokens": 5,
        "messages": [{"role": "user", "content": "Say hello in five words."}],
        **extra,
    }


async def post(app, payload: dict | bytes) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mock"
    ) as h:
        if isinstance(payload, bytes):
            return await h.post("/v1/chat/completions", content=payload)
        return await h.post("/v1/chat/completions", json=payload)


def events(response: httpx.Response) -> list[dict]:
    return [e for e in (parse_line(line) for line in response.text.splitlines()) if e is not None]


async def test_a_stream_sends_max_tokens_chunks_then_usage_then_done() -> None:
    response = await post(
        create_app(FAST), body(stream=True, stream_options={"include_usage": True})
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    chunks = events(response)
    content = [c for c in chunks if c["choices"] and c["choices"][0]["delta"].get("content")]
    assert len(content) == 5
    assert chunks[-1]["usage"] == {"prompt_tokens": 6, "completion_tokens": 5, "total_tokens": 11}
    assert chunks[-1]["choices"] == []
    assert response.text.rstrip().endswith("data: [DONE]")
    assert all(c["model"] == "mock-model" for c in chunks)


async def test_a_stream_without_include_usage_has_no_usage_chunk() -> None:
    chunks = events(await post(create_app(FAST), body(stream=True)))

    assert not any(c.get("usage") for c in chunks)


async def test_a_plain_request_gets_the_whole_answer_and_usage() -> None:
    response = await post(create_app(FAST), body(max_tokens=7))

    reply = response.json()
    assert reply["usage"] == {"prompt_tokens": 6, "completion_tokens": 7, "total_tokens": 13}
    assert reply["choices"][0]["message"]["role"] == "assistant"


async def test_tokens_come_at_the_set_pace() -> None:
    app = create_app(MockSettings(tokens_per_s=100.0, first_token_s=0.05))

    start = time.perf_counter()
    await post(app, body(stream=True, max_tokens=10))

    assert time.perf_counter() - start >= 0.05 + 0.09  # first token, then 9 more at 10 ms each


async def test_metrics_look_like_llama_server_to_the_gateways_load_poller() -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(FAST)),
                                 base_url="http://mock") as h:  # fmt: skip
        response = await h.get("/metrics")
        health = await h.get("/health")

    assert response.status_code == 200
    assert "llamacpp:requests_processing 0" in response.text
    assert "llamacpp:requests_deferred 0" in response.text
    assert health.status_code == 200


async def test_a_body_that_is_not_json_is_a_400() -> None:
    response = await post(create_app(FAST), b"not json")

    assert response.status_code == 400
    assert "error" in json.loads(response.content)


async def test_an_unknown_path_is_a_404() -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(FAST)),
                                 base_url="http://mock") as h:  # fmt: skip
        assert (await h.get("/v1/models/nope")).status_code == 404
