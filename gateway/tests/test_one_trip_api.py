"""Redis round trips before the model is contacted (ADR 0021), through the whole gateway.

Redis is on another node for half the gateways, so every trip before the first token shows in
time to first token. A local request with a free slot makes one: ADMIT reserves its estimate,
joins the fair queue and takes the slot. A hosted request makes one: RESERVE. A request the router
turns away makes none.
"""

import asyncio
from collections.abc import AsyncIterator, Callable

import fakeredis
import httpx
import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio

CLAUDE = "claude-opus-5-5"


class SpyRedis(fakeredis.FakeAsyncRedis):
    """An in-memory Redis that records the name of every command sent to it."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.commands: list[str] = []

    async def execute_command(self, *args: object, **options: object) -> object:
        self.commands.append(str(args[0]).upper())
        return await super().execute_command(*args, **options)


@pytest.fixture
def redis() -> SpyRedis:  # replaces conftest's, for every gateway in this module
    return SpyRedis()


def watch(server, redis: SpyRedis) -> list[list[str]]:
    """For each request a model server (local or hosted) gets: the Redis commands sent before it.
    Call before the gateway is built: it wraps the fake server's handler."""
    seen: list[list[str]] = []
    original = server.handle

    async def handle(request):
        if request.url.path != "/metrics":
            seen.append(list(redis.commands))
        return await original(request)

    server.handle = handle
    return seen


@pytest_asyncio.fixture
async def run(make_gateway, support_key, engineering_key) -> AsyncIterator[Callable]:
    """run(**settings) -> (app, post): a running gateway, and post(tenant, body) to call it."""
    stack = []
    keys = {"support": support_key, "engineering": engineering_key}

    async def build(**settings: object):
        app = make_gateway(**settings)
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://g")
        stack.append((lifespan, http))

        async def post(tenant: str, body: dict) -> httpx.Response:
            headers = {"Authorization": f"Bearer {keys[tenant]}"}
            return await http.post("/v1/chat/completions", json=body, headers=headers)

        return app, post

    yield build
    for lifespan, http in reversed(stack):
        await http.aclose()
        await lifespan.__aexit__(None, None, None)


async def warm_up(post: Callable, redis: SpyRedis, tenant: str, body: dict) -> None:
    """One request first: Redis loads each Lua script once (SCRIPT LOAD), then only runs it."""
    assert (await post(tenant, body)).status_code == 200
    redis.commands.clear()


async def test_a_local_request_with_a_free_slot_makes_one_redis_call_before_the_model(
    run, upstream, redis, chat
) -> None:
    seen = watch(upstream, redis)
    _, post = await run()
    await warm_up(post, redis, "support", chat())

    response = await post("support", chat())

    assert response.status_code == 200
    assert seen[-1] == ["EVALSHA"]  # ADMIT: reserve, join, start


async def test_a_streamed_local_request_too(run, upstream, redis, chat) -> None:
    seen = watch(upstream, redis)
    _, post = await run()
    await warm_up(post, redis, "support", chat(stream=True))

    response = await post("support", chat(stream=True))

    assert response.status_code == 200
    assert seen[-1] == ["EVALSHA"]


async def test_a_hosted_request_makes_one_redis_call_before_the_model(
    run, claude, redis, chat
) -> None:
    seen = watch(claude, redis)
    _, post = await run()
    await warm_up(post, redis, "engineering", chat(model=CLAUDE))

    response = await post("engineering", chat(model=CLAUDE))

    assert response.status_code == 200
    assert seen[-1] == ["EVALSHA"]  # RESERVE: no queue for the hosted model


async def test_a_request_the_router_turns_away_makes_no_redis_call(run, redis, chat) -> None:
    _, post = await run()
    await warm_up(post, redis, "support", chat())

    response = await post("support", chat(model=CLAUDE))  # support's data stays local

    assert response.status_code == 403
    assert redis.commands == []  # no reservation to make, and none to give back


async def test_a_full_queue_turns_a_request_away_without_charging_it(run, upstream, chat) -> None:
    one_slot = [{"name": "node-1", "url": "http://upstream", "kind": "llama", "slots": 1}]
    app, post = await run(backends=one_slot, queue_max_waiting=1)
    limit = app.state.settings.limit_for("standard")
    upstream.delay = 0.3
    running = asyncio.create_task(post("support", chat()))
    await asyncio.sleep(0.05)
    waiting = asyncio.create_task(post("support", chat()))
    await asyncio.sleep(0.05)
    level = await app.state.buckets.level("support", limit)

    response = await post("support", chat())

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "queue_full"
    assert await app.state.buckets.level("support", limit) == level  # its estimate went back once
    assert [r.status_code for r in await asyncio.gather(running, waiting)] == [200, 200]
