"""Routing through the whole gateway: which backend answers, and the data policy (ADR 0013).

Three local backends (vLLM, node-1, node-2) on one fake transport, told apart by host name, plus the
fake Anthropic API. Tenants: support is local_only, engineering is hosted_allowed.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable

import anthropic
import httpx
import httpx2
import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio

BACKENDS = [
    {"name": "vllm", "url": "http://vllm:8000", "kind": "vllm", "slots": 32, "priority": 0},
    {"name": "node-1", "url": "http://node-1:8080", "kind": "llama", "slots": 2, "priority": 1},
    {"name": "node-2", "url": "http://node-2:8080", "kind": "llama", "slots": 2, "priority": 1},
]
CLAUDE = "claude-opus-5-5"


class Gateway:
    def __init__(self, app, http: httpx.AsyncClient, keys: dict[str, str]) -> None:
        self.app, self.http, self.keys = app, http, keys

    async def poll(self) -> None:
        await self.app.state.loads.poll_once()

    async def chat(self, tenant: str, **body: object) -> httpx.Response:
        payload = {"model": "smollm3-3b", "messages": [{"role": "user", "content": "Hi"}], **body}
        headers = {"Authorization": f"Bearer {self.keys[tenant]}"}
        return await self.http.post("/v1/chat/completions", json=payload, headers=headers)

    async def models(self, tenant: str) -> list[str]:
        headers = {"Authorization": f"Bearer {self.keys[tenant]}"}
        response = await self.http.get("/v1/models", headers=headers)
        return [m["id"] for m in response.json()["data"]]


@pytest_asyncio.fixture
async def make(make_gateway, support_key, engineering_key) -> AsyncIterator[Callable]:
    """make(**settings) -> a running Gateway with the three backends."""
    stack = []

    async def build(**settings: object) -> Gateway:
        settings.setdefault("backends", BACKENDS)
        app = make_gateway(**settings)
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://g")
        stack.append((lifespan, http))
        return Gateway(app, http, {"support": support_key, "engineering": engineering_key})

    yield build
    for lifespan, http in reversed(stack):
        await http.aclose()
        await lifespan.__aexit__(None, None, None)


@pytest_asyncio.fixture
async def gateway(make) -> Gateway:
    return await make()


def chat_hosts(upstream) -> list[str]:
    return [r.url.host for r in upstream.requests]


def full(upstream) -> None:
    upstream.busy.update({"vllm": 32, "node-1": 2, "node-2": 3})


# --- local routing -------------------------------------------------------------------------------


async def test_requests_go_to_the_preferred_backend(gateway, upstream) -> None:
    await gateway.poll()

    response = await gateway.chat("support")

    assert response.status_code == 200
    assert response.headers["x-tollgate-backend"] == "vllm"
    assert chat_hosts(upstream) == ["vllm"]


async def test_a_full_backend_is_skipped(gateway, upstream) -> None:
    upstream.busy.update({"vllm": 32, "node-1": 1})
    await gateway.poll()

    response = await gateway.chat("support")

    assert response.headers["x-tollgate-backend"] == "node-2"


async def test_a_down_backend_is_skipped(gateway, upstream) -> None:
    upstream.down.add("vllm")  # the workstation is off
    await gateway.poll()

    response = await gateway.chat("support")

    assert response.headers["x-tollgate-backend"] in {"node-1", "node-2"}


async def test_a_burst_between_two_polls_is_spread(gateway, upstream) -> None:
    # A request counts against its backend the moment it is sent, not when the backend answers,
    # or a burst would all land on whichever backend looked idlest at the last poll.
    upstream.down.add("vllm")
    await gateway.poll()
    upstream.delay = 0.05

    responses = await asyncio.gather(*(gateway.chat("support") for _ in range(4)))

    backends = sorted(r.headers["x-tollgate-backend"] for r in responses)
    assert backends == ["node-1", "node-1", "node-2", "node-2"]


async def test_a_refused_connection_fails_over_to_the_next_backend(gateway, upstream) -> None:
    await gateway.poll()  # vLLM looked fine a moment ago...
    upstream.down.add("vllm")  # ...and has just gone away

    response = await gateway.chat("support")

    assert response.status_code == 200
    assert chat_hosts(upstream) == ["vllm", "node-1"]
    assert response.headers["x-tollgate-backend"] == "node-1"
    loads = {load.name: load for load in gateway.app.state.loads.snapshot()}
    assert not loads["vllm"].up  # skipped by later requests until a poll finds it again


async def test_the_backend_api_key_goes_to_local_backends(make, upstream) -> None:
    gateway = await make(upstream_api_key="vllm-secret")

    await gateway.chat("support")

    assert upstream.requests[-1].headers["authorization"] == "Bearer vllm-secret"


# --- the data policy -----------------------------------------------------------------------------


async def test_local_only_never_reaches_the_hosted_model_even_when_everything_is_full(
    gateway, upstream, claude
) -> None:
    full(upstream)
    await gateway.poll()

    response = await gateway.chat("support")

    assert response.status_code == 200  # it waits in a local queue instead
    assert (
        response.headers["x-tollgate-backend"] == "vllm"
    )  # full too, but preferred: its queue moves fastest
    assert claude.requests == []


async def test_local_only_with_no_local_backend_gets_503_not_the_hosted_model(
    gateway, upstream, claude
) -> None:
    upstream.down.update({"vllm", "node-1", "node-2"})
    await gateway.poll()

    response = await gateway.chat("support")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "no_backend_available"
    assert claude.requests == []


async def test_local_only_asking_for_the_hosted_model_is_403(gateway, upstream, claude) -> None:
    response = await gateway.chat("support", model=CLAUDE)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "data_policy"
    assert claude.requests == [] and upstream.requests == []


# --- the hosted model ----------------------------------------------------------------------------


async def test_hosted_allowed_overflows_when_everything_is_full(gateway, upstream, claude) -> None:
    full(upstream)
    await gateway.poll()

    response = await gateway.chat("engineering")

    assert response.status_code == 200
    assert response.headers["x-tollgate-backend"] == "hosted"
    reply = response.json()
    assert reply["model"] == CLAUDE
    assert reply["choices"][0]["message"]["content"] == "Hello"
    assert upstream.requests == []
    assert len(claude.requests) == 1


async def test_hosted_allowed_stays_local_while_there_is_room(gateway, upstream, claude) -> None:
    await gateway.poll()

    response = await gateway.chat("engineering")

    assert response.headers["x-tollgate-backend"] == "vllm"
    assert claude.requests == []


async def test_asking_for_the_hosted_model_goes_there(gateway, upstream, claude) -> None:
    response = await gateway.chat("engineering", model=CLAUDE, temperature=0.5)

    assert response.status_code == 200
    assert response.headers["x-tollgate-backend"] == "hosted"
    assert "temperature" not in claude.last_body
    assert upstream.requests == []


async def test_a_hosted_stream_is_openai_sse(gateway) -> None:
    response = await gateway.chat("engineering", model=CLAUDE, stream=True)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [line[6:] for line in response.text.split("\n\n") if line.startswith("data: ")]
    chunks = [json.loads(e) for e in events[:-1]]
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks) == "Hello"
    assert all(c["choices"] for c in chunks)  # the usage chunk is gone: the client didn't ask
    assert events[-1] == "[DONE]"


async def test_a_hosted_stream_keeps_usage_when_asked(gateway) -> None:
    response = await gateway.chat(
        "engineering", model=CLAUDE, stream=True, stream_options={"include_usage": True}
    )

    events = [line[6:] for line in response.text.split("\n\n") if line.startswith("data: ")]
    assert json.loads(events[-2])["usage"]["total_tokens"] == 19


async def test_hosted_usage_settles_the_rate_limit(make, redis, clock) -> None:
    from tollgate_gateway.limits import Limit, TokenBuckets

    gateway = await make(rate_limits={"standard": 10_000})

    await gateway.chat("engineering", model=CLAUDE, max_tokens=500)

    level = await TokenBuckets(redis, clock=clock).level("engineering", Limit(10_000))
    assert level == pytest.approx(10_000 - 19)  # 12 prompt + 7 output, as Anthropic reported


async def test_a_refused_hosted_request_refunds_the_reservation(make, redis, clock) -> None:
    from tollgate_gateway.limits import Limit, TokenBuckets

    gateway = await make(rate_limits={"standard": 10_000})

    await gateway.chat("support", model=CLAUDE, max_tokens=500)

    level = await TokenBuckets(redis, clock=clock).level("support", Limit(10_000))
    assert level == pytest.approx(10_000)


def anthropic_error(status: int, type_: str) -> httpx2.Response:
    return httpx2.Response(status, json={"type": "error", "error": {"type": type_, "message": "x"}})


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (anthropic_error(529, "overloaded_error"), 503, "hosted_unavailable"),
        (anthropic_error(429, "rate_limit_error"), 503, "hosted_unavailable"),
        (anthropic_error(400, "invalid_request_error"), 400, "hosted_rejected"),
        (anthropic_error(401, "authentication_error"), 502, "hosted_failed"),
        (anthropic_error(500, "api_error"), 502, "hosted_failed"),
        (httpx2.ConnectError("refused"), 503, "hosted_unavailable"),
        (httpx2.ReadTimeout("slow"), 504, "hosted_timeout"),
    ],
)
async def test_hosted_failures_become_openai_errors(
    make, claude, failure, status: int, code: str
) -> None:
    gateway = await make(hosted_max_retries=0)
    claude.script = [failure]

    response = await gateway.chat("engineering", model=CLAUDE)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code


async def test_no_api_key_means_no_hosted_model(make, upstream, claude) -> None:
    gateway = await make(anthropic_api_key=None)
    full(upstream)
    await gateway.poll()

    asked = await gateway.chat("engineering", model=CLAUDE)
    overflow = await gateway.chat("engineering")

    assert asked.status_code == 404
    assert overflow.headers["x-tollgate-backend"] == "vllm"  # queued locally instead
    assert claude.requests == []


async def test_requests_the_hosted_model_cannot_take_do_not_overflow(
    gateway, upstream, claude
) -> None:
    full(upstream)
    await gateway.poll()

    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {}}}]
    response = await gateway.chat("engineering", tools=tools)

    assert response.headers["x-tollgate-backend"] == "vllm"
    assert claude.requests == []


# --- /v1/models ----------------------------------------------------------------------------------


async def test_models_list_the_hosted_model_only_for_tenants_allowed_to_use_it(gateway) -> None:
    assert await gateway.models("support") == ["smollm3-3b"]
    assert await gateway.models("engineering") == ["smollm3-3b", CLAUDE]


async def test_the_sdk_error_classes_are_the_ones_we_map() -> None:
    # Guard against an SDK upgrade renaming what hosted_failure() relies on.
    assert issubclass(anthropic.APITimeoutError, anthropic.APIConnectionError)
    assert issubclass(anthropic.RateLimitError, anthropic.APIStatusError)
