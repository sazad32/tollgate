"""The fair queue against an in-memory Redis that runs the real Lua scripts (ADR 0014).

Slots are handed out one at a time: each free slot goes to the waiting tenant with the fewest
requests running, ties to whoever's next request has waited longest; first come, first served
within a tenant.
"""

import fakeredis
import pytest
import pytest_asyncio
from tollgate_gateway.queue import FairQueue, retry_after_s

pytestmark = pytest.mark.asyncio


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest_asyncio.fixture
async def queue(clock):
    redis = fakeredis.FakeAsyncRedis()
    yield FairQueue(redis, clock=clock, lease_s=30, max_waiting=3)
    await redis.aclose()


async def start(queue: FairQueue, tenant: str, request_id: str, capacity: int) -> str:
    """Join, then try once: "start" or "wait"."""
    await queue.join(tenant, request_id)
    return await queue.try_start(tenant, request_id, capacity)


async def running(queue: FairQueue) -> dict[str, int]:
    state = await queue.state()
    return state.running


# --- capacity ------------------------------------------------------------------------------------


async def test_a_request_starts_while_there_is_a_free_slot(queue) -> None:
    assert await start(queue, "support", "s1", capacity=2) == "start"
    assert await start(queue, "support", "s2", capacity=2) == "start"
    assert await running(queue) == {"support": 2}


async def test_a_request_waits_when_every_slot_is_taken(queue) -> None:
    await start(queue, "support", "s1", capacity=1)

    assert await start(queue, "support", "s2", capacity=1) == "wait"


async def test_a_finished_request_frees_its_slot(queue) -> None:
    await start(queue, "support", "s1", capacity=1)
    await queue.join("support", "s2")

    await queue.finish("support", "s1")

    assert await queue.try_start("support", "s2", capacity=1) == "start"


async def test_finishing_twice_is_harmless(queue) -> None:
    await start(queue, "support", "s1", capacity=1)

    await queue.finish("support", "s1")
    await queue.finish("support", "s1")

    assert await running(queue) == {}


async def test_more_capacity_lets_waiting_requests_start(queue) -> None:
    # The workstation came back: vLLM's 32 slots join the nodes' 4.
    await start(queue, "support", "s1", capacity=1)
    assert await start(queue, "support", "s2", capacity=1) == "wait"

    assert await queue.try_start("support", "s2", capacity=36) == "start"


# --- fairness ------------------------------------------------------------------------------------


async def test_within_a_tenant_first_come_first_served(queue) -> None:
    await start(queue, "support", "s1", capacity=1)
    await queue.join("support", "s2")
    await queue.join("support", "s3")
    await queue.finish("support", "s1")

    assert await queue.try_start("support", "s3", capacity=1) == "wait"  # s2 is ahead
    assert await queue.try_start("support", "s2", capacity=1) == "start"


async def test_the_tenant_with_fewest_running_goes_next(queue) -> None:
    # support floods; billing arrives later, but has nothing running, so it goes first.
    for i in range(2):
        await start(queue, "support", f"s{i}", capacity=2)
    await queue.join("support", "s-late")
    await queue.join("billing", "b1")

    await queue.finish("support", "s0")

    assert await queue.try_start("support", "s-late", capacity=2) == "wait"
    assert await queue.try_start("billing", "b1", capacity=2) == "start"


async def test_a_tie_goes_to_whoever_waited_longest(queue) -> None:
    await start(queue, "support", "s1", capacity=2)
    await start(queue, "billing", "b1", capacity=2)
    await queue.join("billing", "b2")  # waiting first
    await queue.join("support", "s2")

    await queue.finish("support", "s1")
    await queue.finish("billing", "b1")  # 0 running each now

    assert await queue.try_start("support", "s2", capacity=1) == "wait"
    assert await queue.try_start("billing", "b2", capacity=1) == "start"


async def test_several_free_slots_are_shared_out_in_one_go(queue) -> None:
    # Two free slots, billing has none running and support has one: both slots can't go to
    # support's queue, so one support request and one billing request start.
    await start(queue, "support", "s0", capacity=3)
    for rid in ("s1", "s2"):
        await queue.join("support", rid)
    await queue.join("billing", "b1")

    assert await queue.try_start("support", "s1", capacity=3) == "start"
    assert await queue.try_start("support", "s2", capacity=3) == "wait"
    assert await queue.try_start("billing", "b1", capacity=3) == "start"


async def test_one_busy_tenant_may_use_every_slot(queue) -> None:
    # Fair, but not wasteful: with nobody else waiting, support gets all the capacity.
    results = [await start(queue, "support", f"s{i}", capacity=3) for i in range(3)]

    assert results == ["start", "start", "start"]


# --- limits on waiting ---------------------------------------------------------------------------


async def test_a_tenant_can_only_have_so_many_waiting(queue) -> None:
    await start(queue, "support", "s0", capacity=1)
    for i in range(3):
        assert (await queue.join("support", f"s{i + 1}")).accepted

    refused = await queue.join("support", "s4")

    assert not refused.accepted
    assert refused.waiting == 3


async def test_another_tenants_queue_is_not_affected(queue) -> None:
    await start(queue, "support", "s0", capacity=1)
    for i in range(3):
        await queue.join("support", f"s{i + 1}")

    assert (await queue.join("billing", "b1")).accepted


async def test_join_reports_the_queue_in_front(queue) -> None:
    await start(queue, "support", "s0", capacity=1)
    await queue.join("support", "s1")

    joined = await queue.join("billing", "b1")

    assert (joined.waiting, joined.waiting_total, joined.running_total) == (1, 2, 1)


async def test_leaving_the_queue_gives_up_the_place(queue) -> None:
    await start(queue, "support", "s0", capacity=1)
    await queue.join("support", "s1")
    await queue.join("support", "s2")

    await queue.finish("support", "s1")  # the client hung up while waiting
    await queue.finish("support", "s0")

    assert await queue.try_start("support", "s2", capacity=1) == "start"


# --- leases --------------------------------------------------------------------------------------


async def test_an_unrenewed_slot_expires(queue, clock) -> None:
    # The gateway pod holding it died: its slot comes back after the lease.
    await start(queue, "support", "s1", capacity=1)
    await queue.join("billing", "b1")

    clock.now += 31

    assert await queue.try_start("billing", "b1", capacity=1) == "gone"  # its own lease ran out
    assert await running(queue) == {}


async def test_renewing_keeps_a_slot(queue, clock) -> None:
    await start(queue, "support", "s1", capacity=1)

    clock.now += 20
    await queue.renew([("support", "s1")])
    clock.now += 20

    assert await running(queue) == {"support": 1}


async def test_a_dead_waiter_does_not_block_the_queue(queue, clock) -> None:
    await start(queue, "support", "s0", capacity=1)
    await queue.join("billing", "b-dead")  # its pod dies; nobody renews it
    clock.now += 20
    await queue.join("support", "s1")
    await queue.renew([("support", "s0"), ("support", "s1")])
    clock.now += 15  # b-dead is now past its lease
    await queue.finish("support", "s0")

    assert await queue.try_start("support", "s1", capacity=1) == "start"


async def test_renewing_something_finished_does_not_bring_it_back(queue) -> None:
    await start(queue, "support", "s1", capacity=1)
    await queue.finish("support", "s1")

    await queue.renew([("support", "s1")])

    assert await running(queue) == {}


# --- the retry hint ------------------------------------------------------------------------------


async def test_retry_after_is_when_the_queue_in_front_should_be_gone() -> None:
    # 7 waiting in front plus this one, 4 slots, each held ~10 s: about 20 s.
    assert retry_after_s(waiting_total=7, capacity=4, avg_hold_s=10) == 20


async def test_retry_after_stays_between_1_s_and_5_minutes() -> None:
    assert retry_after_s(waiting_total=0, capacity=36, avg_hold_s=0.1) == 1
    assert retry_after_s(waiting_total=500, capacity=1, avg_hold_s=60) == 300
    assert retry_after_s(waiting_total=3, capacity=0, avg_hold_s=10) == 40  # no capacity: as 1
