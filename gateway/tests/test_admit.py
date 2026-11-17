"""One Redis round trip before the first token (ADR 0021), against an in-memory Redis that runs the
real Lua scripts.

ADMIT replaces three calls: RESERVE (the token bucket), JOIN and TRY_START (the fair queue), with
the estimate going back into the bucket if the queue is full. The differential test at the end
checks that it does exactly what those calls did, using frozen copies of the scripts as they were
before ADMIT (week 15), on thousands of generated states.
"""

import random

import fakeredis
import pytest
import pytest_asyncio
from tollgate_gateway.admit import FirstTrip, OneTrip
from tollgate_gateway.limits import Limit, TokenBuckets
from tollgate_gateway.queue import FairQueue

pytestmark = pytest.mark.asyncio

LIMIT = Limit(tokens_per_minute=600)  # capacity 600, refills 10 tokens per second


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def one(slots: int) -> list[tuple[str, int, int]]:
    """A single backend with this many slots: (name, slots, priority)."""
    return [("node-1", slots, 1)]


@pytest_asyncio.fixture
async def parts():
    """(OneTrip, its buckets, its queue) on one in-memory Redis, max 2 waiting per tenant."""
    redis = fakeredis.FakeAsyncRedis()
    clock = Clock()
    buckets = TokenBuckets(redis, clock=clock)
    queue = FairQueue(redis, clock=clock, lease_s=30, max_waiting=2)
    yield OneTrip(buckets, queue), buckets, queue
    await redis.aclose()


# --- what one call does --------------------------------------------------------------------------


async def test_with_a_free_slot_one_call_reserves_and_starts(parts) -> None:
    one_trip, buckets, queue = parts

    first = await one_trip.admit("support", "s1", LIMIT, 100, one(2))

    assert first.outcome == "start"
    assert first.backend == "node-1"
    assert first.decision.allowed
    assert first.decision.remaining == 500
    assert await buckets.level("support", LIMIT) == 500
    assert (await queue.state()).running == {"support": 1}


async def test_a_rate_limited_request_joins_nothing(parts) -> None:
    one_trip, buckets, queue = parts
    await one_trip.admit("support", "s1", LIMIT, 550, one(2))  # leaves 50

    first = await one_trip.admit("support", "s2", LIMIT, 100, one(2))

    assert first.outcome == "limited"
    assert not first.decision.allowed
    assert first.decision.retry_after_s == pytest.approx(5.0)  # 50 more tokens at 10 a second
    assert first.joined is None
    assert await buckets.level("support", LIMIT) == 50  # nothing taken
    state = await queue.state()
    assert state.running == {"support": 1} and state.waiting == {}


async def test_a_full_queue_puts_the_estimate_back(parts) -> None:
    one_trip, buckets, queue = parts
    await one_trip.admit("support", "s1", LIMIT, 100, one(1))  # running
    await one_trip.admit("support", "s2", LIMIT, 100, one(1))  # waiting
    await one_trip.admit("support", "s3", LIMIT, 100, one(1))  # waiting: 2, the most allowed

    first = await one_trip.admit("support", "s4", LIMIT, 100, one(1))

    assert first.outcome == "queue_full"
    assert first.joined is not None and first.joined.waiting == 2
    assert first.decision.remaining == 300
    assert await buckets.level("support", LIMIT) == 300  # s4's 100 went back in
    assert (await queue.state()).waiting == {"support": 2}


async def test_with_no_free_slot_it_waits_keeping_its_reservation(parts) -> None:
    one_trip, buckets, queue = parts
    await one_trip.admit("support", "s1", LIMIT, 100, one(1))

    first = await one_trip.admit("support", "s2", LIMIT, 100, one(1))

    assert first.outcome == "wait"
    assert first.backend is None
    assert await buckets.level("support", LIMIT) == 400
    assert (await queue.state()).waiting == {"support": 1}
    # It goes on waiting as before: TRY_START, every poll, until its turn.
    await queue.finish("support", "s1")
    assert (await queue.try_start("support", "s2", one(1))).outcome == "start"


async def test_it_does_not_jump_the_queue(parts) -> None:
    one_trip, _, queue = parts
    await one_trip.admit("support", "s1", LIMIT, 10, one(1))  # running
    await one_trip.admit("billing", "b1", LIMIT, 10, one(1))  # waiting: next in line
    await queue.finish("support", "s1")  # a slot is free, and it's billing's

    first = await one_trip.admit("support", "s2", LIMIT, 10, one(1))

    assert first.outcome == "wait"
    assert (await queue.try_start("billing", "b1", one(1))).outcome == "start"


async def test_with_no_backend_up_it_waits(parts) -> None:
    one_trip, _, queue = parts

    first = await one_trip.admit("support", "s1", LIMIT, 10, [])

    assert first.outcome == "wait"
    assert (await queue.state()).waiting == {"support": 1}


# --- the same as the three calls it replaces ------------------------------------------------------

# The scripts as they were before ADMIT (week 15), frozen. Don't update these to match the code:
# they're what the code is checked against.
# One thing this can't see: real Redis turns a Lua number into text with 14 digits, fakeredis with
# all of them. So where a script stores `now`, it must pass on ARGV's text as the old ones did
# (try_start's `started`), not tonumber(now); this test would pass either way.
FROZEN_RESERVE = """
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil then
  tokens = capacity
  ts = now
end
if now > ts then
  tokens = math.min(capacity, tokens + (now - ts) * rate)
  ts = now
end

local cost = tonumber(ARGV[4])
local needed = math.min(cost, capacity)
local allowed = 0
local wait = 0
if tokens >= needed then
  tokens = tokens - cost
  allowed = 1
else
  wait = (needed - tokens) / rate
end

redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(ts))
redis.call('EXPIRE', KEYS[1], math.ceil((capacity - tokens) / rate) + 1)
return {allowed, tostring(tokens), tostring(wait)}"""
FROZEN_SETTLE = """
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil then
  tokens = capacity
  ts = now
end
if now > ts then
  tokens = math.min(capacity, tokens + (now - ts) * rate)
  ts = now
end

tokens = math.min(capacity, tokens + tonumber(ARGV[4]))

redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(ts))
redis.call('EXPIRE', KEYS[1], math.ceil((capacity - tokens) / rate) + 1)
return tostring(tokens)"""
FROZEN_JOIN = """
local running, waiting, leases, backend_of = KEYS[1], KEYS[2], KEYS[3], KEYS[4]
local now = tonumber(ARGV[1])
local function prune()
  for _, m in ipairs(redis.call('ZRANGEBYSCORE', leases, '-inf', now)) do
    redis.call('ZREM', running, m)
    redis.call('ZREM', waiting, m)
    redis.call('ZREM', leases, m)
    redis.call('HDEL', backend_of, m)
  end
end
local function tenant_of(m) return string.match(m, '^(%S+) ') end

prune()
local member, expiry, max_waiting = ARGV[2], ARGV[3], tonumber(ARGV[4])
local tenant = tenant_of(member)
local all = redis.call('ZRANGE', waiting, 0, -1)
local mine = 0
for _, m in ipairs(all) do
  if tenant_of(m) == tenant then mine = mine + 1 end
end
local busy = redis.call('ZCARD', running)
if mine >= max_waiting then
  return {0, mine, #all, busy}
end
redis.call('ZADD', waiting, redis.call('INCR', KEYS[5]), member)
redis.call('ZADD', leases, expiry, member)
return {1, mine + 1, #all + 1, busy}
"""
FROZEN_TRY_START = """
local running, waiting, leases, backend_of = KEYS[1], KEYS[2], KEYS[3], KEYS[4]
local now = tonumber(ARGV[1])
local function prune()
  for _, m in ipairs(redis.call('ZRANGEBYSCORE', leases, '-inf', now)) do
    redis.call('ZREM', running, m)
    redis.call('ZREM', waiting, m)
    redis.call('ZREM', leases, m)
    redis.call('HDEL', backend_of, m)
  end
end
local function tenant_of(m) return string.match(m, '^(%S+) ') end

prune()
local member, expiry = ARGV[2], ARGV[3]
if not redis.call('ZSCORE', waiting, member) then
  if redis.call('ZSCORE', running, member) then
    return {'start', redis.call('HGET', backend_of, member) or ''}
  end
  return {'gone', ''}  -- its lease ran out, or it left
end

local names, slots, priority = {}, {}, {}
for i = 4, #ARGV, 3 do
  local n = ARGV[i]
  table.insert(names, n)
  slots[n], priority[n] = tonumber(ARGV[i + 1]), tonumber(ARGV[i + 2])
end

-- What runs where: per backend, and per tenant.
local on, count = {}, {}
for _, m in ipairs(redis.call('ZRANGE', running, 0, -1)) do
  local t = tenant_of(m)
  count[t] = (count[t] or 0) + 1
  local b = redis.call('HGET', backend_of, m)
  if b then on[b] = (on[b] or 0) + 1 end
end
local free = 0
for _, n in ipairs(names) do free = free + math.max(0, slots[n] - (on[n] or 0)) end
if free <= 0 then return {'wait', ''} end

-- Each tenant's waiting requests, in arrival order.
local queues, tenants = {}, {}
local all = redis.call('ZRANGE', waiting, 0, -1, 'WITHSCORES')
for i = 1, #all, 2 do
  local m, arrival = all[i], tonumber(all[i + 1])
  local t = tenant_of(m)
  if not queues[t] then
    queues[t] = {}
    table.insert(tenants, t)
  end
  table.insert(queues[t], {m, arrival})
end
local head = {}
for _, t in ipairs(tenants) do head[t] = 1 end

-- Hand out the free slots one by one. If this member would get one, it starts now.
for _ = 1, free do
  local best, best_count, best_arrival = nil, nil, nil
  for _, t in ipairs(tenants) do
    local next_up = queues[t][head[t]]
    if next_up then
      local c = count[t] or 0
      if best == nil or c < best_count or (c == best_count and next_up[2] < best_arrival) then
        best, best_count, best_arrival = t, c, next_up[2]
      end
    end
  end
  if best == nil then break end
  if queues[best][head[best]][1] == member then
    -- Its slot: a backend with room, the preferred kind first, then the one with most room.
    local pick, pick_free = nil, nil
    for _, n in ipairs(names) do
      local f = slots[n] - (on[n] or 0)
      if f > 0 and (pick == nil or priority[n] < priority[pick]
          or (priority[n] == priority[pick] and (f > pick_free or (f == pick_free and n < pick))))
      then
        pick, pick_free = n, f
      end
    end
    redis.call('ZREM', waiting, member)
    redis.call('ZADD', running, ARGV[1], member)
    redis.call('ZADD', leases, expiry, member)
    redis.call('HSET', backend_of, member, pick)
    return {'start', pick}
  end
  head[best] = head[best] + 1
  count[best] = best_count + 1
end
return {'wait', ''}
"""

TENANTS = ("support", "billing", "engineering")
SEEDS = range(10)
STATES_PER_SEED = 60


def random_state(rng: random.Random, now: float) -> dict:
    """A bucket, a queue and a request, covering the cases that matter: an empty, full, low or
    indebted bucket; a clock behind the bucket's; running requests on backends that are up, full or
    gone; waiting requests; leases that ran out; tenants at the waiting limit."""
    tenant = rng.choice(TENANTS)
    capacity = rng.choice([600, 1000])
    bucket = None
    if rng.random() < 0.8:
        bucket = (rng.uniform(-200, capacity), now - rng.uniform(-2, 120))
    backends = [(f"b{i}", rng.randint(0, 3), rng.randint(0, 2)) for i in range(rng.randint(0, 3))]
    names = [n for n, _, _ in backends] + ["gone"]
    running = [
        (rng.choice(TENANTS), f"r{i}", rng.choice(names), now + rng.uniform(-10, 30))
        for i in range(rng.randint(0, 6))
    ]
    waiting = [
        (rng.choice(TENANTS), f"w{i}", now + rng.uniform(-10, 30)) for i in range(rng.randint(0, 6))
    ]
    return {
        "tenant": tenant,
        "limit": Limit(tokens_per_minute=capacity),
        "cost": rng.randint(1, capacity * 3 // 2),
        "bucket": bucket,
        "backends": backends,
        "running": running,
        "waiting": waiting,
        "max_waiting": rng.randint(1, 3),
    }


async def load(redis: fakeredis.FakeAsyncRedis, state: dict, now: float) -> None:
    if state["bucket"] is not None:
        tokens, ts = state["bucket"]
        await redis.hset(f"tollgate:bucket:{state['tenant']}", mapping={"tokens": tokens, "ts": ts})
    for tenant, request_id, backend, lease in state["running"]:
        member = f"{tenant} {request_id}"
        await redis.zadd("tollgate:queue:running", {member: now - 5})
        await redis.zadd("tollgate:queue:leases", {member: lease})
        await redis.hset("tollgate:queue:backend", member, backend)
    for arrival, (tenant, request_id, lease) in enumerate(state["waiting"], start=1):
        member = f"{tenant} {request_id}"
        await redis.zadd("tollgate:queue:waiting", {member: arrival})
        await redis.zadd("tollgate:queue:leases", {member: lease})
    await redis.set("tollgate:queue:arrivals", len(state["waiting"]))


async def dump(redis: fakeredis.FakeAsyncRedis) -> dict:
    """Everything in Redis: each key's type, contents and time to live in ms (-1: none)."""
    out = {}
    for key in sorted(await redis.keys("*")):
        kind = (await redis.type(key)).decode()
        if kind == "hash":
            value = sorted((await redis.hgetall(key)).items())
        elif kind == "zset":
            value = await redis.zrange(key, 0, -1, withscores=True)
        else:
            value = await redis.get(key)
        out[key.decode()] = (kind, value, await redis.pttl(key))
    return out


def same(a: dict, b: dict) -> bool:
    """Equal, but a time to live may differ by part of a second: EXPIRE counts from the real
    clock, which moves on between the runs. A whole second apart is a different EXPIRE."""
    if a.keys() != b.keys():
        return False
    return all(a[k][:2] == b[k][:2] and abs(a[k][2] - b[k][2]) < 500 for k in a)


def parts_on(redis, now: float, max_waiting: int, frozen: bool):
    clock = Clock(now)
    buckets = TokenBuckets(redis, clock=clock)
    queue = FairQueue(redis, clock=clock, lease_s=30, max_waiting=max_waiting)
    if frozen:
        buckets._reserve = redis.register_script(FROZEN_RESERVE)
        buckets._settle = redis.register_script(FROZEN_SETTLE)
        queue._join = redis.register_script(FROZEN_JOIN)
        queue._try_start = redis.register_script(FROZEN_TRY_START)
    return buckets, queue


async def three_calls(buckets: TokenBuckets, queue: FairQueue, state: dict) -> tuple:
    """What a local request did before ADMIT, step by step, as (outcome, decision, backend,
    joined)."""
    tenant, limit, cost = state["tenant"], state["limit"], state["cost"]
    decision = await buckets.reserve(tenant, limit, cost)
    if not decision.allowed:
        return "limited", decision, None, None
    joined = await queue.join(tenant, "new")
    if not joined.accepted:
        level = await buckets.settle(tenant, limit, reserved=cost, actual=0)
        refunded = type(decision)(True, level, 0.0, limit)
        return "queue_full", refunded, None, joined
    turn = await queue.try_start(tenant, "new", state["backends"])
    return turn.outcome, decision, turn.backend, joined


def as_tuple(first: FirstTrip) -> tuple:
    return first.outcome, first.decision, first.backend, first.joined


@pytest.mark.parametrize("seed", SEEDS)
async def test_admit_does_exactly_what_the_three_calls_did(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(STATES_PER_SEED):
        now = 1_000_000.0 + rng.uniform(0, 1000)
        state = random_state(rng, now)
        frozen, split, single = (fakeredis.FakeAsyncRedis() for _ in range(3))
        try:
            for redis in (frozen, split, single):
                await load(redis, state, now)

            # Before ADMIT: the frozen scripts, three calls.
            expected = await three_calls(*parts_on(frozen, now, state["max_waiting"], True), state)
            # Today's RESERVE, JOIN and TRY_START, rebuilt from the shared pieces: still the same.
            assert (
                await three_calls(*parts_on(split, now, state["max_waiting"], False), state)
                == expected
            ), state
            # ADMIT: one call.
            buckets, queue = parts_on(single, now, state["max_waiting"], False)
            first = await OneTrip(buckets, queue).admit(
                state["tenant"], "new", state["limit"], state["cost"], state["backends"]
            )
            assert as_tuple(first) == expected, state

            before = await dump(frozen)
            assert same(await dump(split), before), state
            assert same(await dump(single), before), state
        finally:
            for redis in (frozen, split, single):
                await redis.aclose()
