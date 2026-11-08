"""Waiting for a local slot, through the whole gateway (ADR 0014).

One backend with 2 slots, so a third request has to wait. The fake model server takes `delay`
seconds per request and records the most requests it was ever answering at once (`peak`).
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable

import fakeredis
import httpx
import pytest
import pytest_asyncio
from tollgate_gateway.config import Settings
from tollgate_gateway.main import create_app

pytestmark = pytest.mark.asyncio


class Gateway:
    def __init__(self, app, http: httpx.AsyncClient, keys: dict[str, str]) -> None:
        self.app, self.http, self.keys = app, http, keys

    async def chat(self, tenant: str, tag: str = "", **body: object) -> httpx.Response:
        payload = {
            "model": "smollm3-3b",
            "messages": [{"role": "user", "content": f"{tenant}:{tag}"}],
            **body,
        }
        headers = {"Authorization": f"Bearer {self.keys[tenant]}"}
        return await self.http.post("/v1/chat/completions", json=payload, headers=headers)

    async def queue_state(self):
        return await self.app.state.admission.queue.state()


@pytest_asyncio.fixture
async def make(make_gateway, support_key, billing_key) -> AsyncIterator[Callable]:
    """make(**settings) -> a running Gateway. Gateways made in one test share one Redis."""
    stack = []

    async def build(**settings: object) -> Gateway:
        app = make_gateway(**settings)
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://g")
        stack.append((lifespan, http))
        return Gateway(app, http, {"support": support_key, "billing": billing_key})

    yield build
    for lifespan, http in reversed(stack):
        await http.aclose()
        await lifespan.__aexit__(None, None, None)


def arrival_order(upstream) -> list[str]:
    """Which tenant's request reached the model server first, second..."""
    return [json.loads(r.content)["messages"][0]["content"] for r in upstream.requests]


# --- waiting for a slot --------------------------------------------------------------------------


async def test_requests_beyond_the_slots_wait_their_turn(make, upstream) -> None:
    gateway = await make()
    upstream.delay = 0.2

    responses = await asyncio.gather(*(gateway.chat("support", str(i)) for i in range(5)))

    assert [r.status_code for r in responses] == [200] * 5
    assert upstream.peak == 2  # never more than the backend's 2 slots at once


async def test_a_flooding_tenant_does_not_make_others_wait_behind_it(make, upstream) -> None:
    gateway = await make()
    upstream.delay = 0.2
    flood = [asyncio.create_task(gateway.chat("support", str(i))) for i in range(6)]
    await asyncio.sleep(0.05)  # support's 6 are in: 2 running, 4 waiting

    await gateway.chat("billing", "1")
    await asyncio.gather(*flood)

    # billing arrived 7th, but starts in the next wave: when both of support's slots free up at
    # once, support (0 running again, and waiting longer) gets one and billing the other.
    # First come, first served would have made it wait for all 6.
    order = arrival_order(upstream)
    assert order.index("billing:1") <= 3
    assert order[-1] != "billing:1"


async def test_with_the_queue_off_requests_go_straight_to_the_backend(make, upstream) -> None:
    gateway = await make(fair_queue=False)
    upstream.delay = 0.2

    await asyncio.gather(*(gateway.chat("support", str(i)) for i in range(5)))

    assert upstream.peak == 5  # all at once: they'd queue inside llama-server instead


async def test_the_slot_is_given_back_after_every_kind_of_ending(make, upstream) -> None:
    gateway = await make()

    await gateway.chat("support", "plain")
    await gateway.chat("support", "stream", stream=True)
    upstream.script = [httpx.Response(500, text="boom")]
    await gateway.chat("support", "server error")
    upstream.script = [httpx.ConnectError("refused"), httpx.ConnectError("refused")]
    await gateway.chat("support", "unreachable")

    state = await gateway.queue_state()
    assert (state.running, state.waiting) == ({}, {})


async def test_two_gateway_replicas_share_one_queue(make, upstream) -> None:
    first, second = await make(), await make()  # same Redis, same backend
    upstream.delay = 0.2

    await asyncio.gather(
        *(first.chat("support", f"a{i}") for i in range(3)),
        *(second.chat("support", f"b{i}") for i in range(3)),
    )

    assert upstream.peak == 2  # each replica alone would have allowed 2


# --- 429 with a retry hint -----------------------------------------------------------------------


async def test_a_full_queue_is_429_with_retry_after(make, upstream, redis, clock) -> None:
    from tollgate_gateway.limits import Limit, TokenBuckets

    gateway = await make(queue_max_waiting=1, rate_limits={"standard": 10_000})
    upstream.delay = 0.3
    busy = [asyncio.create_task(gateway.chat("support", str(i))) for i in range(3)]
    await asyncio.sleep(0.05)  # 2 running, 1 waiting: support's queue is full

    refused = await gateway.chat("support", "one too many", max_tokens=100)
    await asyncio.gather(*busy)

    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "queue_full"
    assert int(refused.headers["retry-after"]) >= 1
    # Its reservation was refunded: only the 3 served requests are charged.
    level = await TokenBuckets(redis, clock=clock).level("support", Limit(10_000))
    served = 3 * 14  # the fake's usage report: 14 tokens per reply
    assert level == pytest.approx(10_000 - served)


async def test_another_tenant_still_gets_in_when_one_queue_is_full(make, upstream) -> None:
    gateway = await make(queue_max_waiting=1)
    upstream.delay = 0.3
    busy = [asyncio.create_task(gateway.chat("support", str(i))) for i in range(3)]
    await asyncio.sleep(0.05)

    response = await gateway.chat("billing", "1")
    await asyncio.gather(*busy)

    assert response.status_code == 200


async def test_waiting_too_long_is_429(make, upstream, clock) -> None:
    gateway = await make(queue_max_wait_s=5)
    upstream.delay = 0.5
    busy = [asyncio.create_task(gateway.chat("support", str(i))) for i in range(2)]
    await asyncio.sleep(0.05)
    waiter = asyncio.create_task(gateway.chat("support", "patient"))
    await asyncio.sleep(0.05)

    clock.now += 6
    response = await waiter
    await asyncio.gather(*busy)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "queue_timeout"
    assert "support:patient" not in arrival_order(upstream)
    assert (await gateway.queue_state()).waiting == {}


# --- a client that leaves while queued -----------------------------------------------------------


async def test_a_client_that_leaves_while_queued_gives_up_its_place(make, upstream, support_key):
    gateway = await make()
    upstream.delay = 0.3
    busy = [asyncio.create_task(gateway.chat("support", str(i))) for i in range(2)]
    await asyncio.sleep(0.05)

    # Drive the app like Uvicorn would: the request arrives, then the client hangs up.
    body = {"model": "smollm3-3b", "messages": [{"role": "user", "content": "support:gone"}]}
    messages = [{"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}]

    async def receive() -> dict:
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        pass

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions", "query_string": b"", "root_path": "",
        "headers": [(b"content-type", b"application/json"), (b"host", b"g"),
                    (b"authorization", f"Bearer {support_key}".encode())],
        "client": ("127.0.0.1", 50000), "server": ("g", 80),
    }  # fmt: skip
    await asyncio.wait_for(gateway.app(scope, receive, send), timeout=2)  # returns while queued
    await asyncio.gather(*busy)

    assert "support:gone" not in arrival_order(upstream)
    state = await gateway.queue_state()
    assert (state.running, state.waiting) == ({}, {})


# --- Redis down ----------------------------------------------------------------------------------


async def test_redis_down_means_no_queue_not_no_service(upstream, auth_store, support_key):
    server = fakeredis.FakeServer()
    server.connected = False
    app = create_app(
        Settings(
            backends=[{"name": "node-1", "url": "http://upstream", "slots": 1}],
            load_poll_interval_s=0,
            api_key_pepper="test-pepper",
        ),
        upstream_transport=httpx.MockTransport(upstream.handle),
        auth_store=auth_store,
        redis_client=fakeredis.FakeAsyncRedis(server=server),
    )
    upstream.delay = 0.1
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    headers = {"Authorization": f"Bearer {support_key}"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://g") as http,
    ):
        responses = await asyncio.gather(
            *(http.post("/v1/chat/completions", json=body, headers=headers) for _ in range(3))
        )

    assert [r.status_code for r in responses] == [200, 200, 200]  # fail open


# --- the renewal backstop ------------------------------------------------------------------------


async def test_a_slot_held_impossibly_long_stops_being_renewed(clock) -> None:
    # If a release never runs (a bug, or a stream that never started), the replica must not keep
    # the slot alive forever: past max_hold_s it stops renewing, and the lease runs out.
    from tollgate_gateway.admission import Admission
    from tollgate_gateway.queue import FairQueue

    redis = fakeredis.FakeAsyncRedis()
    queue = FairQueue(redis, clock=clock, lease_s=0.03)
    admission = Admission(queue, clock, max_hold_s=100)

    async def connected() -> bool:
        return False

    ticket = await admission.wait_for_slot("support", "r1", lambda: 1, connected)
    renewals = asyncio.create_task(admission.run_renewals())
    clock.now += 101
    await asyncio.sleep(0.05)
    renewals.cancel()

    assert ("support", "r1") not in admission.held
    clock.now += 1
    assert (await queue.state()).running == {}
    assert ticket.queued
    await redis.aclose()
