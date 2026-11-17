"""What the gateway does when it's overloaded or Redis struggles (ADR 0020, from the week 14 load
test): wait briefly for Redis instead of failing at once, count every fail-open, retry releasing a
queue slot, and refuse quickly past a cap on requests in flight instead of running out of memory.
"""

import asyncio
import json

import fakeredis
import httpx
import pytest
from redis.asyncio import BlockingConnectionPool
from tollgate_gateway.admission import Admission
from tollgate_gateway.config import Settings
from tollgate_gateway.main import create_app, make_redis
from tollgate_gateway.metrics import GatewayMetrics, InFlightLimit
from tollgate_gateway.queue import FairQueue

pytestmark = pytest.mark.asyncio


# --- the Redis pool -------------------------------------------------------------------------------


async def test_redis_connections_come_from_a_pool_that_waits_briefly_when_all_are_busy() -> None:
    settings = Settings(api_key_pepper="x", redis_url="redis://:pw@redis:6379/0",
                        redis_max_connections=300, redis_pool_timeout_s=0.25)  # fmt: skip
    redis = make_redis(settings)
    try:
        pool = redis.connection_pool
        # redis-py's default pool fails at once past 100 connections: under load, every rate limit
        # and queue step then failed open (week 14).
        assert isinstance(pool, BlockingConnectionPool)
        assert pool.max_connections == 300
        assert pool.timeout == 0.25
    finally:
        await redis.aclose()


# --- counting fail-opens --------------------------------------------------------------------------


async def test_every_fail_open_is_counted(upstream, auth_store, support_key) -> None:
    server = fakeredis.FakeServer()
    server.connected = False
    app = create_app(
        Settings(backends=[{"name": "node-1", "url": "http://upstream", "slots": 2}],
                 load_poll_interval_s=0, api_key_pepper="test-pepper", upstream_pools=1),
        upstream_transport=httpx.MockTransport(upstream.handle), auth_store=auth_store,
        redis_client=fakeredis.FakeAsyncRedis(server=server),
    )  # fmt: skip
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://g") as http,
    ):
        response = await http.post("/v1/chat/completions", json=body,
                                   headers={"Authorization": f"Bearer {support_key}"})  # fmt: skip
        metrics = (await http.get("/metrics")).text

    assert response.status_code == 200  # still fails open: no Redis is no limits, not no service
    assert 'tollgate_gateway_redis_fail_open_total{step="rate_limit"} 1.0' in metrics
    assert 'tollgate_gateway_redis_fail_open_total{step="queue"} 1.0' in metrics


# --- releasing a queue slot -----------------------------------------------------------------------


async def test_a_slot_that_could_not_be_released_is_retried_not_left_to_its_lease(clock) -> None:
    server = fakeredis.FakeServer()
    redis = fakeredis.FakeAsyncRedis(server=server)
    queue = FairQueue(redis, clock=clock, lease_s=30.0)
    failed_steps: list[str] = []
    admission = Admission(queue, clock, on_fail_open=failed_steps.append, retry_release_s=0.02)

    async def connected() -> bool:
        return False

    ticket = await admission.wait_for_slot("support", "r1", lambda: [("node-1", 1, 1)], connected)
    server.connected = False  # Redis busy or gone at the moment the request ends
    await ticket.release()
    assert failed_steps == ["queue_release"]

    server.connected = True
    await asyncio.sleep(0.1)  # a few retries later, long before the 30 s lease

    assert (await queue.state()).running == {}
    assert admission.pending_releases == 0
    await redis.aclose()


async def test_retrying_a_release_gives_up_when_the_lease_would_have_freed_it(clock) -> None:
    server = fakeredis.FakeServer()
    redis = fakeredis.FakeAsyncRedis(server=server)
    queue = FairQueue(redis, clock=clock, lease_s=30.0)
    admission = Admission(queue, clock, retry_release_s=0.01)

    async def connected() -> bool:
        return False

    ticket = await admission.wait_for_slot("support", "r1", lambda: [("node-1", 1, 1)], connected)
    server.connected = False
    await ticket.release()
    clock.now += 31  # the lease has run out by itself
    await asyncio.sleep(0.05)

    assert admission.pending_releases == 0
    await redis.aclose()


# --- a cap on requests in flight ------------------------------------------------------------------


class Held:
    """An ASGI app that answers only when the test says so."""

    def __init__(self) -> None:
        self.go = asyncio.Event()

    async def __call__(self, scope, receive, send) -> None:
        await self.go.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def call(app, path: str = "/v1/chat/completions") -> list[dict]:
    sent: list[dict] = []

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        sent.append(message)

    await app({"type": "http", "path": path, "method": "POST"}, receive, send)
    return sent


async def test_past_the_cap_a_request_is_refused_at_once_with_retry_after() -> None:
    inner = Held()
    app = InFlightLimit(inner, max_in_flight=1)

    first = asyncio.create_task(call(app))
    await asyncio.sleep(0.01)
    second = await asyncio.wait_for(call(app), timeout=1)  # no waiting: refused straight away

    start, body = second
    assert start["status"] == 503
    assert (b"retry-after", b"1") in start["headers"]
    error = json.loads(body["body"])["error"]
    assert (error["type"], error["code"]) == ("server_error", "overloaded")

    inner.go.set()
    assert (await first)[0]["status"] == 200
    assert app.in_flight == 0


async def test_the_cap_frees_up_when_a_request_ends_however_it_ends() -> None:
    class Fails:
        async def __call__(self, scope, receive, send):
            raise RuntimeError("broke")

    app = InFlightLimit(Fails(), max_in_flight=1)
    with pytest.raises(RuntimeError):
        await call(app)

    assert app.in_flight == 0


async def test_only_model_requests_count_against_the_cap() -> None:
    inner = Held()
    app = InFlightLimit(inner, max_in_flight=1)
    first = asyncio.create_task(call(app))
    await asyncio.sleep(0.01)
    inner.go.set()  # let everything through from now on

    assert (await call(app, "/healthz"))[0]["status"] == 200
    assert (await call(app, "/metrics"))[0]["status"] == 200
    await first


async def test_no_cap_by_default(make_gateway) -> None:
    assert make_gateway().state.settings.max_in_flight is None


async def test_refusals_are_counted_like_any_response(make_gateway, support_key, chat) -> None:
    app = make_gateway(max_in_flight=0)  # nothing gets in: every model request is refused
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://g") as http,
    ):
        response = await http.post("/v1/chat/completions", json=chat(),
                                   headers={"Authorization": f"Bearer {support_key}"})  # fmt: skip
        metrics = (await http.get("/metrics")).text

    assert (response.status_code, response.headers["retry-after"]) == (503, "1")
    assert 'tollgate_gateway_requests_total{status="503"} 1.0' in metrics
    assert isinstance(app.state.metrics, GatewayMetrics)
