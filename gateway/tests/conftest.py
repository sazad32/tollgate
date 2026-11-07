"""Shared test setup: the gateway in front of a fake model server. No network, no real model.

.NET: like WebApplicationFactory<Program> with the HttpClient's handler swapped for a fake one.
"""

import json
from collections.abc import AsyncIterator, Callable

import httpx
import pytest
import pytest_asyncio
from tollgate_gateway.config import Settings
from tollgate_gateway.main import create_app

CHUNKS = [
    {"choices": [{"delta": {"role": "assistant"}}]},
    {"choices": [{"delta": {"content": "Hel"}}]},
    {"choices": [{"delta": {"content": "lo"}}]},
    {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2}},
]
SSE_BODY = "".join(f"data: {json.dumps(c)}\n\n" for c in CHUNKS).encode() + b"data: [DONE]\n\n"
COMPLETION = {"choices": [{"message": {"role": "assistant", "content": "Hello"}}], "usage": {}}


class FakeUpstream:
    """A model server that answers from a script: each entry is a Response or an exception."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.script: list[httpx.Response | Exception] = []

    def default(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "smollm3-3b"}]})
        if json.loads(request.content).get("stream"):
            return httpx.Response(
                200, content=SSE_BODY, headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(200, json=COMPLETION)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            return step
        return self.default(request)

    @property
    def last_body(self) -> dict:
        return json.loads(self.requests[-1].content)


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream()


@pytest.fixture
def make_gateway(upstream: FakeUpstream) -> Callable:
    def build(**settings: object):
        settings.setdefault("upstream_url", "http://upstream")
        return create_app(Settings(**settings), httpx.MockTransport(upstream.handle))

    return build


@pytest_asyncio.fixture
async def client(make_gateway: Callable) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client talking to the gateway in-process, with its startup/shutdown run."""
    app = make_gateway()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as http,
    ):
        yield http


@pytest.fixture
def chat() -> Callable[..., dict]:
    """Build a chat-completion request body: chat(stream=True, max_tokens=5, ...)."""

    def build(stream: bool = False, **extra: object) -> dict:
        return {
            "model": "smollm3-3b",
            "messages": [{"role": "user", "content": "Say hello."}],
            "stream": stream,
            **extra,
        }

    return build


@pytest.fixture
def sse_body() -> bytes:
    return SSE_BODY


@pytest.fixture
def completion() -> dict:
    return COMPLETION
