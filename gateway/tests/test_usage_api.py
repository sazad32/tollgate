"""Usage events from requests through the whole gateway (ADR 0015).

One event per request a model ran, with the numbers the client saw; none for requests that never
reached a model. Events are read back from the (fake) Redis stream.
"""

import asyncio
import json

import fakeredis
import httpx
import httpx2
import pytest
from tollgate_gateway.limits import estimate_tokens, prompt_estimate
from tollgate_gateway.usage import STREAM, UsageEvent

pytestmark = pytest.mark.asyncio

CLAUDE = "claude-opus-5-5"
NOW = 1_000_000.0  # the test clock (conftest.Clock)


@pytest.fixture
def server() -> fakeredis.FakeServer:
    return fakeredis.FakeServer()


@pytest.fixture
def redis(server) -> fakeredis.FakeAsyncRedis:
    """Overrides conftest's, so a test can take this Redis down and bring it back."""
    return fakeredis.FakeAsyncRedis(server=server)


async def events(redis, count: int = 1, wait_s: float = 1.0) -> list[UsageEvent]:
    """The events in the stream, once there are `count` (or wait_s has passed)."""
    deadline = asyncio.get_running_loop().time() + wait_s
    while True:
        entries = await redis.xrange(STREAM)
        if len(entries) >= count or asyncio.get_running_loop().time() > deadline:
            return [UsageEvent.from_fields(fields) for _, fields in entries]
        await asyncio.sleep(0.01)


def expected(request_id: str, **changes: object) -> UsageEvent:
    fields: dict = {
        "request_id": request_id,
        "tenant_id": "support",
        "credential": "key:key-support",
        "model": "smollm3-3b",
        "backend": "node-1",
        "input_tokens": 9,
        "output_tokens": 5,
        "token_source": "reported",
        "status": "completed",
        "stream": False,
        "started_at": NOW,
        "finished_at": NOW,
    }
    fields.update(changes)
    return UsageEvent(**fields)


# --- one event per request a model ran -----------------------------------------------------------


async def test_a_reply_gives_one_event_with_the_reported_tokens(client, redis, chat) -> None:
    response = await client.post("/v1/chat/completions", json=chat())

    assert await events(redis) == [expected(response.headers["x-request-id"])]


async def test_a_stream_gives_one_event_too(client, redis, chat) -> None:
    response = await client.post("/v1/chat/completions", json=chat(stream=True))

    assert await events(redis) == [
        expected(response.headers["x-request-id"], output_tokens=2, stream=True)
    ]


async def test_a_retried_request_is_still_one_event(client, redis, upstream, chat) -> None:
    upstream.script = [httpx.RemoteProtocolError("closed before responding")]

    response = await client.post("/v1/chat/completions", json=chat())

    assert len(upstream.requests) == 2  # the gateway retried once
    found = await events(redis, 2, wait_s=0.3)
    assert [e.idempotency_key for e in found] == [response.headers["x-request-id"] + ":usage"]


async def test_a_stream_that_breaks_is_failed_and_estimated(client, redis, upstream, chat):
    class OneChunkThenBreak(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices": [{"delta": {"content": "Hi"}}]}\n\n'
            raise httpx.ReadError("connection reset")

    upstream.script = [
        httpx.Response(
            200, stream=OneChunkThenBreak(), headers={"content-type": "text/event-stream"}
        )
    ]
    body = chat(stream=True)

    response = await client.post("/v1/chat/completions", json=body)

    assert await events(redis) == [
        expected(
            response.headers["x-request-id"], input_tokens=prompt_estimate(body), output_tokens=1,
            token_source="estimated", status="failed", stream=True,
        )
    ]  # fmt: skip


async def test_a_reply_without_a_usage_report_is_charged_its_estimate(
    client, redis, upstream, chat
) -> None:
    upstream.script = [httpx.Response(200, json={"choices": [{"message": {"content": "Hi"}}]})]
    body = chat(max_tokens=50)

    response = await client.post("/v1/chat/completions", json=body)

    assert await events(redis) == [
        expected(
            response.headers["x-request-id"], input_tokens=prompt_estimate(body),
            output_tokens=estimate_tokens(body) - prompt_estimate(body), token_source="estimated",
        )
    ]  # fmt: skip


# --- no event when no model ran ------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(400, json={"error": {"message": "prompt too long"}}),
        httpx.Response(500, text="boom"),
        httpx.ConnectError("refused"),
    ],
)
async def test_a_request_the_model_did_not_run_gives_no_event(
    client, redis, upstream, chat, failure
) -> None:
    upstream.script = [failure, failure]

    response = await client.post("/v1/chat/completions", json=chat())

    assert response.status_code >= 400
    assert await events(redis, wait_s=0.2) == []


async def test_refused_requests_give_no_event(anonymous, client, redis, chat) -> None:
    unauthenticated = await anonymous.post(
        "/v1/chat/completions", json=chat(), headers={"Authorization": "Bearer tg_nope"}
    )
    data_policy = await client.post("/v1/chat/completions", json=chat(model=CLAUDE))

    assert (unauthenticated.status_code, data_policy.status_code) == (401, 403)
    assert await events(redis, wait_s=0.2) == []


async def test_a_rate_limited_request_gives_no_event(
    make_gateway, support_key, redis, upstream, chat
) -> None:
    upstream.delay = 0.2  # both in flight at once: the first's reservation is still held
    app = make_gateway(rate_limits={"standard": 600})
    body = chat(max_tokens=400)  # reserves ~500 of the 600: a second one doesn't fit
    headers = {"Authorization": f"Bearer {support_key}"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://g") as http,
    ):
        first, second = await asyncio.gather(
            http.post("/v1/chat/completions", json=body, headers=headers),
            http.post("/v1/chat/completions", json=body, headers=headers),
        )

    assert sorted([first.status_code, second.status_code]) == [200, 429]
    assert len(await events(redis, 2, wait_s=0.3)) == 1


# --- the hosted model ----------------------------------------------------------------------------


async def hosted_call(make_gateway, engineering_key, chat, **body: object) -> httpx.Response:
    app = make_gateway()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://g") as http,
    ):
        response = await http.post(
            "/v1/chat/completions",
            json=chat(model=CLAUDE, **body),
            headers={"Authorization": f"Bearer {engineering_key}"},
        )
        await asyncio.sleep(0.05)  # let the emitter write before the app shuts down
        return response


@pytest.mark.parametrize("stream", [False, True])
async def test_a_hosted_request_records_the_model_that_answered(
    make_gateway, engineering_key, redis, chat, stream
) -> None:
    response = await hosted_call(make_gateway, engineering_key, chat, stream=stream)

    assert await events(redis) == [
        expected(
            response.headers["x-request-id"], tenant_id="engineering",
            credential="key:key-engineering", model=CLAUDE, backend="hosted", input_tokens=12,
            output_tokens=7, stream=stream,
        )
    ]  # fmt: skip


async def test_a_refusal_fallback_records_every_attempt(
    make_gateway, engineering_key, redis, chat, claude
) -> None:
    claude.iterations = [
        {"type": "message", "input_tokens": 12, "output_tokens": 4},
        {"type": "fallback_message", "input_tokens": 12, "output_tokens": 7},
    ]

    await hosted_call(make_gateway, engineering_key, chat)

    [event] = await events(redis)
    assert (event.input_tokens, event.output_tokens) == (24, 11)


async def test_a_hosted_failure_gives_no_event(
    make_gateway, engineering_key, redis, chat, claude
) -> None:
    claude.script = [
        httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error",
                                                                "message": "too long"}})
    ]  # fmt: skip

    response = await hosted_call(make_gateway, engineering_key, chat)

    assert response.status_code == 400
    assert await events(redis, wait_s=0.2) == []


# --- Redis down ----------------------------------------------------------------------------------


async def test_with_redis_down_requests_work_and_events_arrive_later(
    client, redis, server, chat
) -> None:
    server.connected = False

    response = await client.post("/v1/chat/completions", json=chat())
    assert response.status_code == 200  # billing is never in the request path

    server.connected = True
    found = await events(redis)
    assert [e.request_id for e in found] == [response.headers["x-request-id"]]


async def test_each_event_is_valid_json_friendly(client, redis, chat) -> None:
    # Week 11's consumer reads these: every field is a plain string.
    await client.post("/v1/chat/completions", json=chat())
    await events(redis)

    [(_, fields)] = await redis.xrange(STREAM)
    assert json.loads(json.dumps({k.decode(): v.decode() for k, v in fields.items()}))
