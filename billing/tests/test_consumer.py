"""The billing consumer (ADR 0016) against fake Redis and an in-memory ledger.

The guarantee under test: every event in the stream ends up in the ledger exactly once, whatever
fails and whenever, and an entry leaves the stream only after the ledger has it.

.NET: like testing a BackgroundService that reads a Service Bus subscription with peek-lock: the
"pending" list is the set of locked messages, XACK is Complete().
"""

import asyncio
import uuid

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from tollgate_billing.consumer import Consumer

pytestmark = pytest.mark.asyncio

STREAM = "tollgate:usage"
GROUP = "billing"


@pytest.fixture
def consumer(redis, ledger):
    def make(name: str = "billing-a", **options) -> Consumer:
        options = {"block_ms": 10, "retry_s": 0.01, **options}
        return Consumer(redis, ledger, stream=STREAM, group=GROUP, name=name, **options)

    return make


@pytest.fixture
def send(redis, event_fields):
    """What the gateway does: XADD one event. Returns its stream entry ID."""

    async def add(**overrides: str) -> str:
        overrides.setdefault("request_id", uuid.uuid4().hex)
        entry_id = await redis.xadd(STREAM, event_fields(**overrides))
        return entry_id.decode()

    return add


async def pending(redis) -> int:
    return (await redis.xpending(STREAM, GROUP))["pending"]


async def eventually(check) -> bool:
    for _ in range(300):
        if check():
            return True
        await asyncio.sleep(0.01)
    return check()


# --- reading and writing -------------------------------------------------------------------------


async def test_events_already_in_the_stream_are_billed(redis, ledger, consumer, send) -> None:
    # Week 10's events were written before billing existed: the group starts at the beginning.
    for _ in range(3):
        await send()

    assert await consumer().step() == 3

    assert len(ledger.rows) == 3
    assert await pending(redis) == 0


async def test_an_event_is_acknowledged_only_after_the_ledger_has_it(redis, ledger, consumer, send):
    await send()
    ledger.fail = ConnectionRefusedError("postgres is down")

    with pytest.raises(ConnectionRefusedError):
        await consumer().step()

    assert ledger.rows == {}
    assert await pending(redis) == 1  # still owed to billing


async def test_a_failed_batch_is_retried_before_anything_new(redis, ledger, consumer, send) -> None:
    c = consumer()
    first = await send()
    ledger.fail = ConnectionRefusedError("postgres is down")
    with pytest.raises(ConnectionRefusedError):
        await c.step()
    await send()  # arrives while Postgres is down
    ledger.fail = None

    assert await c.step() == 1  # the retry: only the entry it already had
    assert [e.stream_id for e in ledger.rows.values()] == [first]
    assert await c.step() == 1  # then the new one
    assert len(ledger.rows) == 2


async def test_a_crash_between_commit_and_ack_bills_once(redis, ledger, consumer, send) -> None:
    # The ledger committed, then Redis went away before XACK: the entry is delivered again.
    await send()
    c = consumer()
    real_xack = redis.xack

    async def lost_ack(*args):
        raise RedisConnectionError("connection lost")

    redis.xack = lost_ack
    with pytest.raises(RedisConnectionError):
        await c.step()
    redis.xack = real_xack

    assert await c.step() == 1  # delivered again
    assert len(ledger.rows) == 1  # but billed once
    assert (c.inserted, c.duplicates) == (1, 1)
    assert await pending(redis) == 0


async def test_a_copy_in_the_stream_is_billed_once(redis, ledger, consumer, send) -> None:
    # The gateway's retry wrote the same event twice (ADR 0015: at least once).
    request_id = uuid.uuid4().hex
    await send(request_id=request_id)
    await send(request_id=request_id)
    c = consumer()

    await c.step()

    assert len(ledger.rows) == 1
    assert (c.inserted, c.duplicates) == (1, 1)


async def test_replaying_the_whole_stream_changes_nothing(redis, ledger, consumer, send) -> None:
    for _ in range(4):
        await send()
    c = consumer()
    await c.step()
    before = dict(ledger.rows)

    await redis.xgroup_setid(STREAM, GROUP, "0")  # read everything again
    await c.step()

    assert ledger.rows == before
    assert (c.inserted, c.duplicates) == (4, 4)


async def test_batches_are_capped(redis, consumer, send) -> None:
    for _ in range(5):
        await send()
    c = consumer(batch=2)
    assert [await c.step() for _ in range(4)] == [2, 2, 1, 0]


# --- events billing can't trust ------------------------------------------------------------------


async def test_a_bad_event_is_set_aside_and_the_stream_flows_on(redis, ledger, consumer, send):
    bad = await send(v="2")
    await send()

    c = consumer()
    await c.step()

    assert len(ledger.rows) == 1
    assert list(ledger.rejects) == [bad]
    assert "unsupported schema version" in ledger.rejects[bad].reason
    assert ledger.rejects[bad].fields["v"] == "2"
    assert c.rejected == 1
    assert await pending(redis) == 0  # acknowledged: it won't block the stream


async def test_an_entry_deleted_before_billing_got_it_is_dropped(redis, ledger, consumer, send):
    entry = await send()
    c = consumer()
    ledger.fail = ConnectionRefusedError("postgres is down")
    with pytest.raises(ConnectionRefusedError):
        await c.step()
    await redis.xdel(STREAM, entry)  # someone deleted it by hand while it was pending
    ledger.fail = None

    assert await c.step() == 1
    assert ledger.rows == {} and ledger.rejects == {}
    assert c.gone == 1
    assert await pending(redis) == 0


# --- consumers that die ------------------------------------------------------------------------


async def test_a_dead_consumers_entries_are_taken_over(redis, ledger, consumer, send) -> None:
    await send()
    await send()
    ledger.fail = ConnectionRefusedError("postgres is down")
    with pytest.raises(ConnectionRefusedError):
        await consumer("billing-old-pod").step()  # read both, then the pod died
    ledger.fail = None

    new = consumer("billing-new-pod", claim_idle_ms=0)
    assert await new.step() == 2

    assert len(ledger.rows) == 2
    assert await pending(redis) == 0


async def test_a_live_consumers_entries_are_left_alone(redis, ledger, consumer, send) -> None:
    await send()
    ledger.fail = ConnectionRefusedError("postgres is down")
    with pytest.raises(ConnectionRefusedError):
        await consumer("billing-a").step()
    ledger.fail = None

    # Pending for milliseconds, not the minute that marks a consumer dead.
    assert await consumer("billing-b", claim_idle_ms=60_000).step() == 0
    assert ledger.rows == {}


# --- trimming and housekeeping -------------------------------------------------------------------


async def test_old_acknowledged_entries_are_trimmed(redis, consumer, send) -> None:
    for _ in range(3):
        await send()
    now = [0.0]  # the test's clock, in seconds
    c = consumer(retention_s=3600, clock=lambda: now[0])
    await c.step()

    now[0] = (await _newest_ms(redis)) / 1000 + 10  # 10 s later: inside the window
    await c.maintain()
    assert await redis.xlen(STREAM) == 3

    now[0] = (await _newest_ms(redis)) / 1000 + 3601  # past the window
    await c.maintain()
    assert await redis.xlen(STREAM) == 0


async def test_unacknowledged_entries_are_never_trimmed(redis, ledger, consumer, send) -> None:
    await send()
    c = consumer(retention_s=0, clock=lambda: 4_000_000_000.0)
    ledger.fail = ConnectionRefusedError("postgres is down")
    with pytest.raises(ConnectionRefusedError):
        await c.step()

    await c.maintain()

    assert await redis.xlen(STREAM) == 1


async def test_undelivered_entries_are_never_trimmed(redis, consumer, send) -> None:
    c = consumer(retention_s=0, clock=lambda: 4_000_000_000.0)
    await c.step()  # creates the group, reads nothing
    await send()

    await c.maintain()

    assert await redis.xlen(STREAM) == 1


async def test_dead_consumers_without_pending_entries_are_forgotten(redis, ledger, consumer, send):
    await send()
    await consumer("billing-old-pod").step()  # read and acknowledged; then the pod went away
    await send()
    ledger.fail = ConnectionRefusedError("postgres is down")
    with pytest.raises(ConnectionRefusedError):
        await consumer("billing-stuck-pod").step()  # still owes one: must be kept
    ledger.fail = None

    current = consumer("billing-current", stale_consumer_s=0, claim_idle_ms=60_000)
    await current.maintain()

    names = {c["name"].decode() for c in await redis.xinfo_consumers(STREAM, GROUP)}
    assert names == {"billing-stuck-pod"}


# --- the loop ------------------------------------------------------------------------------------


async def test_the_loop_bills_events_as_they_arrive(redis, ledger, consumer, send) -> None:
    task = asyncio.create_task(consumer().run())
    try:
        await send()
        await send()
        assert await eventually(lambda: len(ledger.rows) == 2)
    finally:
        task.cancel()


async def test_the_loop_waits_out_a_postgres_outage(redis, ledger, consumer, send) -> None:
    ledger.fail = ConnectionRefusedError("postgres is down")
    task = asyncio.create_task(consumer().run())
    try:
        await send()
        await asyncio.sleep(0.1)
        assert ledger.rows == {}
        ledger.fail = None
        assert await eventually(lambda: len(ledger.rows) == 1)
    finally:
        task.cancel()


async def test_the_loop_waits_out_a_redis_outage(redis_server, ledger, consumer, send) -> None:
    c = consumer()
    task = asyncio.create_task(c.run())
    try:
        await send()
        assert await eventually(lambda: len(ledger.rows) == 1)
        redis_server.connected = False
        await asyncio.sleep(0.1)
        redis_server.connected = True
        await send()
        assert await eventually(lambda: len(ledger.rows) == 2)
    finally:
        task.cancel()


async def test_the_loop_recreates_its_group_if_the_stream_was_deleted(
    redis, ledger, consumer, send
):
    task = asyncio.create_task(consumer().run())
    try:
        await send()
        assert await eventually(lambda: len(ledger.rows) == 1)
        await redis.delete(STREAM)  # the group goes with it
        await send()  # a fresh stream, no group
        assert await eventually(lambda: len(ledger.rows) == 2)
    finally:
        task.cancel()


async def test_the_loop_touches_its_heartbeat(consumer, tmp_path) -> None:
    beat = tmp_path / "alive"
    task = asyncio.create_task(consumer(heartbeat=beat).run())
    try:
        assert await eventually(lambda: beat.exists())
    finally:
        task.cancel()


async def _newest_ms(redis) -> int:
    [(entry_id, _)] = await redis.xrevrange(STREAM, count=1)
    return int(entry_id.decode().split("-")[0])
