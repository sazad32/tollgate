"""A client that disconnects mid-stream must stop the model server's work.

This drives the app at the ASGI level, like Uvicorn does, because an HTTP test client can't hang up
halfway through a response.
"""

import asyncio
import json

import httpx
import pytest
from tollgate_gateway.config import Settings
from tollgate_gateway.main import create_app

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


async def test_client_disconnect_closes_the_upstream_stream() -> None:
    stream = HangingStream()

    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

    app = create_app(Settings(upstream_url="http://upstream"), httpx.MockTransport(handle))
    body = json.dumps(
        {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    ).encode()
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
        "headers": [(b"content-type", b"application/json"), (b"host", b"gateway")],
        "client": ("127.0.0.1", 50000),
        "server": ("gateway", 80),
    }
    first_chunk_sent = asyncio.Event()
    pending = [{"type": "http.request", "body": body, "more_body": False}]
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

    assert sent == [FIRST_CHUNK]
    assert stream.closed.is_set()
