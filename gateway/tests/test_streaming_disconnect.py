"""A client that disconnects mid-stream must stop the model server's work.

This drives the app at the ASGI level, like Uvicorn does, because an HTTP test client can't hang up
halfway through a response.
"""

import asyncio
import json

import fakeredis
import httpx
import httpx2
import pytest
from tollgate_gateway.config import Settings
from tollgate_gateway.limits import prompt_estimate
from tollgate_gateway.main import create_app
from tollgate_gateway.usage import STREAM, UsageEvent

pytestmark = pytest.mark.asyncio

FIRST_CHUNK = b'data: {"choices": [{"delta": {"content": "Hi"}}]}\n\n'


class HangingStream(httpx.AsyncByteStream):
    """An upstream body that sends one chunk, then waits forever, like a model mid-generation.

    `aclose` is only called when the gateway closes the upstream response, so `closed` proves the
    gateway released it. The `sleep(0)` inside makes the close itself await, which fails if the
    gateway doesn't shield it from cancellation.
    """

    def __init__(self) -> None:
        self.closed = asyncio.Event()

    async def __aiter__(self):
        yield FIRST_CHUNK
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        await asyncio.sleep(0)
        self.closed.set()


async def disconnect_after_first_chunk(app, body: dict, key: str) -> list[bytes]:
    """Drive the app like Uvicorn would, and hang up as soon as the first body chunk is out.
    Returns the chunks the client received."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},  # what Uvicorn 0.54 reports
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"application/json"),
            (b"host", b"gateway"),
            (b"authorization", f"Bearer {key}".encode()),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("gateway", 80),
    }
    first_chunk_sent = asyncio.Event()
    pending = [{"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}]
    sent: list[bytes] = []

    async def receive() -> dict:
        if pending:
            return pending.pop(0)
        await first_chunk_sent.wait()
        return {"type": "http.disconnect"}  # the user closed the tab

    async def send(message: dict) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            sent.append(message["body"])
            first_chunk_sent.set()

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(app(scope, receive, send), timeout=5)
    return sent


async def test_client_disconnect_closes_the_upstream_stream(auth_store, support_key) -> None:
    stream = HangingStream()

    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

    app = create_app(
        Settings(
            backends=[{"name": "node-1", "url": "http://upstream"}],
            load_poll_interval_s=0,
            api_key_pepper="test-pepper",
        ),
        upstream_transport=httpx.MockTransport(handle),
        auth_store=auth_store,
    )
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True}

    sent = await disconnect_after_first_chunk(app, body, support_key)

    assert sent == [FIRST_CHUNK]
    assert stream.closed.is_set()


class HangingClaude(httpx2.AsyncByteStream):
    """Anthropic's stream: the message starts, one word arrives, then Claude keeps "thinking".

    Unlike HangingStream, `aclose` doesn't await first. When the client leaves, the SDK closes the
    response itself, in an unshielded `finally`; the real transport underneath (httpcore2) shields
    the socket close, so it completes even while cancelled. A fake that awaited first would fail
    where the real stack doesn't.
    """

    def __init__(self) -> None:
        self.closed = asyncio.Event()

    async def __aiter__(self):
        start = {
            "type": "message_start",
            "message": {
                "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
                "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 12, "output_tokens": 1},
            },
        }  # fmt: skip
        word = {"type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": "Hi"}}  # fmt: skip
        for event in (start, word):
            yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed.set()


async def test_client_disconnect_closes_the_hosted_stream(auth_store, engineering_key) -> None:
    # Hosted tokens cost money: a client that leaves must stop Claude, not just stop listening.
    stream = HangingClaude()

    async def handle(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

    app = create_app(
        Settings(
            backends=[{"name": "node-1", "url": "http://upstream"}],
            load_poll_interval_s=0,
            api_key_pepper="test-pepper",
            anthropic_api_key="test-key",
        ),
        hosted_transport=httpx2.MockTransport(handle),
        auth_store=auth_store,
    )
    body = {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}],
            "stream": True}  # fmt: skip

    sent = await disconnect_after_first_chunk(app, body, engineering_key)

    assert sent[0].startswith(b"data: ")  # the client got the start of the reply, then left
    assert stream.closed.is_set()


async def test_a_client_that_leaves_mid_stream_is_recorded_as_such(auth_store, support_key) -> None:
    # Hard problem #1: the usage report never comes, so the event says "estimated", with the
    # prompt estimate and one token for the chunk that was sent (ADR 0015).
    stream = HangingStream()
    redis = fakeredis.FakeAsyncRedis()

    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

    app = create_app(
        Settings(
            backends=[{"name": "node-1", "url": "http://upstream"}],
            load_poll_interval_s=0,
            api_key_pepper="test-pepper",
        ),
        upstream_transport=httpx.MockTransport(handle),
        auth_store=auth_store,
        redis_client=redis,
    )
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True}

    await disconnect_after_first_chunk(app, body, support_key)  # shutdown flushes the events

    [(_, fields)] = await redis.xrange(STREAM)
    event = UsageEvent.from_fields(fields)
    assert (event.status, event.token_source) == ("client_disconnected", "estimated")
    assert (event.input_tokens, event.output_tokens) == (prompt_estimate(body), 1)
