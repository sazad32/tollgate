"""Rate limits as a caller sees them: headers, 429s, settling each kind of ending, failing open."""

import asyncio
import json

import fakeredis
import httpx
import pytest
from tollgate_gateway.config import Settings
from tollgate_gateway.limits import Limit, TokenBuckets, prompt_estimate
from tollgate_gateway.main import create_app

pytestmark = pytest.mark.asyncio

SMALL = 2_000  # tokens per minute for the "limited" tier in these tests


async def gateway_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway")


async def level(redis, tenant: str, tokens_per_minute: int, clock) -> float:
    return await TokenBuckets(redis, clock=clock).level(tenant, Limit(tokens_per_minute))


def heavy_reply() -> httpx.Response:
    """A reply that really used 1,990 tokens, so the bucket stays almost empty after settling."""
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": "..."}}],
            "usage": {"prompt_tokens": 90, "completion_tokens": 1900, "total_tokens": 1990},
        },
    )


@pytest.fixture
def limited(make_gateway):
    """The gateway with the standard tier cut to SMALL tokens per minute."""
    return make_gateway(rate_limits={"standard": SMALL})


async def test_every_response_says_how_much_is_left(client: httpx.AsyncClient, chat) -> None:
    response = await client.post("/v1/chat/completions", json=chat(max_tokens=50))

    assert response.status_code == 200
    assert response.headers["x-ratelimit-limit-tokens"] == "1000000"
    assert int(response.headers["x-ratelimit-remaining-tokens"]) < 1_000_000


async def test_over_the_limit_is_429_with_a_retry_hint(
    limited, support_key, upstream, chat
) -> None:
    auth = {"Authorization": f"Bearer {support_key}"}
    upstream.script = [heavy_reply()]
    async with limited.router.lifespan_context(limited), await gateway_client(limited) as http:
        first = await http.post("/v1/chat/completions", json=chat(max_tokens=1500), headers=auth)
        second = await http.post("/v1/chat/completions", json=chat(max_tokens=1500), headers=auth)

    assert first.status_code == 200
    assert second.status_code == 429
    error = second.json()["error"]
    assert error["code"] == "rate_limit_exceeded"
    assert int(second.headers["retry-after"]) >= 1
    assert second.headers["x-ratelimit-reset-tokens"].endswith("s")
    assert len(upstream.requests) == 1  # the refused request never reached the model


async def test_one_tenant_at_its_limit_doesnt_block_another(
    limited, support_key, make_token, upstream, chat
) -> None:
    upstream.script = [heavy_reply()]
    async with limited.router.lifespan_context(limited), await gateway_client(limited) as http:
        support = {"Authorization": f"Bearer {support_key}"}
        await http.post("/v1/chat/completions", json=chat(max_tokens=1500), headers=support)
        blocked = await http.post(
            "/v1/chat/completions", json=chat(max_tokens=1500), headers=support
        )
        billing = {"Authorization": f"Bearer {make_token(tenant='billing')}"}
        other = await http.post("/v1/chat/completions", json=chat(max_tokens=1500), headers=billing)

    assert blocked.status_code == 429
    assert other.status_code == 200


async def test_a_stream_is_settled_with_the_reported_usage(
    client: httpx.AsyncClient, chat, redis, clock
) -> None:
    await client.post("/v1/chat/completions", json=chat(stream=True, max_tokens=100))

    # Fake upstream reports 9 prompt + 2 completion tokens: only 11 stay spent.
    assert await level(redis, "support", 1_000_000, clock) == pytest.approx(1_000_000 - 11)


async def test_a_plain_reply_is_settled_with_the_reported_usage(
    client: httpx.AsyncClient, chat, redis, clock
) -> None:
    await client.post("/v1/chat/completions", json=chat(max_tokens=100))

    assert await level(redis, "support", 1_000_000, clock) == pytest.approx(1_000_000 - 14)


async def test_usage_is_requested_upstream_but_hidden_from_clients_who_didnt_ask(
    client: httpx.AsyncClient, chat, upstream
) -> None:
    response = await client.post("/v1/chat/completions", json=chat(stream=True))

    assert upstream.last_body["stream_options"]["include_usage"] is True
    assert b'"usage"' not in response.content
    assert response.content.rstrip().endswith(b"data: [DONE]")


async def test_clients_who_asked_for_usage_still_get_it(client: httpx.AsyncClient, chat) -> None:
    body = chat(stream=True, stream_options={"include_usage": True})

    response = await client.post("/v1/chat/completions", json=body)

    assert b'"usage"' in response.content


async def test_an_upstream_failure_refunds_the_reservation(
    client: httpx.AsyncClient, chat, upstream, redis, clock
) -> None:
    upstream.script = [httpx.ConnectError("refused"), httpx.ConnectError("refused")]

    response = await client.post("/v1/chat/completions", json=chat(max_tokens=100))

    assert response.status_code == 503
    assert await level(redis, "support", 1_000_000, clock) == pytest.approx(1_000_000)


async def test_an_upstream_error_reply_refunds_the_reservation(
    client: httpx.AsyncClient, chat, upstream, redis, clock
) -> None:
    upstream.script = [httpx.Response(500, text="boom")]

    await client.post("/v1/chat/completions", json=chat(stream=True, max_tokens=100))

    assert await level(redis, "support", 1_000_000, clock) == pytest.approx(1_000_000)


async def test_redis_down_means_no_limits_not_no_service(
    upstream, auth_store, support_key, chat
) -> None:
    server = fakeredis.FakeServer()
    server.connected = False
    app = create_app(
        Settings(
            backends=[{"name": "node-1", "url": "http://upstream"}],
            load_poll_interval_s=0,
            api_key_pepper="test-pepper",
            rate_limits={"standard": 10},
        ),
        upstream_transport=httpx.MockTransport(upstream.handle),
        auth_store=auth_store,
        redis_client=fakeredis.FakeAsyncRedis(server=server),
    )
    auth = {"Authorization": f"Bearer {support_key}"}
    async with app.router.lifespan_context(app), await gateway_client(app) as http:
        responses = [
            await http.post("/v1/chat/completions", json=chat(max_tokens=500), headers=auth)
            for _ in range(3)
        ]

    assert [r.status_code for r in responses] == [200, 200, 200]  # fail open
    assert "x-ratelimit-remaining-tokens" not in responses[0].headers


async def test_a_client_that_disconnects_mid_stream_pays_for_what_was_sent(
    make_gateway, support_key, redis, clock
) -> None:
    # No usage report arrives, so the gateway counts: prompt estimate + one chunk per token sent.
    class OneChunkThenHang(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices": [{"delta": {"content": "Hi"}}]}\n\n'
            await asyncio.Event().wait()

    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, stream=OneChunkThenHang(), headers={"content-type": "text/event-stream"}
        )

    app = make_gateway()
    app.state.upstream_transport = httpx.MockTransport(handle)
    body = {
        "model": "m",
        "messages": [{"role": "user", "content": "x" * 300}],
        "stream": True,
        "max_tokens": 400,
    }
    sent = asyncio.Event()
    pending = [{"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}]

    async def receive() -> dict:
        if pending:
            return pending.pop(0)
        await sent.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            sent.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"application/json"),
            (b"authorization", f"Bearer {support_key}".encode()),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("gateway", 80),
    }
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(app(scope, receive, send), timeout=5)

    spent = prompt_estimate(body) + 1
    assert await level(redis, "support", 1_000_000, clock) == pytest.approx(1_000_000 - spent)
