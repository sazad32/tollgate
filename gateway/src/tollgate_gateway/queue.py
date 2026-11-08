"""Fair queuing for the local GPUs, in Redis so both gateway replicas share one queue (ADR 0014).

When every local slot is busy, requests wait here instead of in llama-server's first-come queue.
Free slots are handed out one at a time: each goes to the waiting tenant with the fewest requests
running; ties go to whoever's next request has waited longest; within a tenant, first come, first
served. One tenant on its own may use every slot.

A slot is on a particular backend, like a numbered space in a car park: the queue knows exactly
what runs where, so a request is never sent to a backend that is still full. (Choosing from the
polled /metrics instead, which can be a second old, sent requests to a full node, where they
waited in llama-server's own queue for a whole generation.)

Like the token buckets (limits.py), every change is a Lua script, which Redis runs atomically.
Each request, waiting or running, holds a lease its gateway replica renews (admission.py). If the
replica dies, the leases run out and the slots come back by themselves.

.NET: like one SemaphoreSlim per backend, shared by both replicas, that releases waiters by tenant
rather than by arrival and forgets holders that stop checking in.

Redis keys (members are "<tenant> <request id>"):
  running   sorted set: the requests holding a slot
  waiting   sorted set: the waiting requests, scored by arrival number
  leases    sorted set: every member above, scored by when its lease runs out
  backend   hash: running member -> the backend its slot is on
  arrivals  counter for arrival numbers (a counter, not a clock: the replicas' clocks differ)
"""

import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from redis.asyncio import Redis

# Shared by every script: drop members whose lease ran out (their gateway replica is gone).
_PRUNE = """
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
"""

# ARGV: now, member, lease expiry, max waiting per tenant
# -> {accepted (0/1), this tenant's waiting, everyone's waiting, running}
JOIN = (
    _PRUNE
    + """
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
)

# ARGV: now, member, lease expiry, then the backends that are up as triples: name, slots, priority
# -> {'start', backend} | {'wait', ''} | {'gone', ''}
TRY_START = (
    _PRUNE
    + """
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
)

# ARGV: member
FINISH = """
for i = 1, 3 do redis.call('ZREM', KEYS[i], ARGV[1]) end
redis.call('HDEL', KEYS[4], ARGV[1])
return 1
"""

# ARGV: lease expiry, members... Only members still present are renewed (XX: never re-add).
RENEW = """
for i = 2, #ARGV do redis.call('ZADD', KEYS[1], 'XX', ARGV[1], ARGV[i]) end
return 1
"""

# ARGV: now -> {running members, waiting members, the backends of the running ones}
STATE = (
    _PRUNE
    + """
prune()
local running_now = redis.call('ZRANGE', running, 0, -1)
local backends = {}
for _, m in ipairs(running_now) do
  table.insert(backends, redis.call('HGET', backend_of, m) or '')
end
return {running_now, redis.call('ZRANGE', waiting, 0, -1), backends}
"""
)


@dataclass(frozen=True)
class Joined:
    accepted: bool
    waiting: int  # this tenant's waiting requests, including this one if accepted
    waiting_total: int  # everyone's
    running_total: int


@dataclass(frozen=True)
class Turn:
    outcome: str  # start | wait | gone
    backend: str | None = None  # with "start": the backend this request's slot is on


@dataclass(frozen=True)
class QueueState:
    running: dict[str, int]  # tenant -> requests holding a slot
    waiting: dict[str, int]  # tenant -> requests waiting
    backends: dict[str, int]  # backend -> slots in use


def retry_after_s(waiting_total: int, capacity: int, avg_hold_s: float) -> int:
    """When to try again: the time for the queue in front, plus this request, to get through
    `capacity` slots that each free up about every avg_hold_s seconds. 1 s to 5 minutes."""
    estimate = math.ceil(avg_hold_s * (waiting_total + 1) / max(1, capacity))
    return min(300, max(1, estimate))


def _text(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class FairQueue:
    def __init__(
        self,
        redis: Redis,
        clock: Callable[[], float],
        lease_s: float = 30.0,
        max_waiting: int = 16,
        key_prefix: str = "tollgate:queue:",
    ) -> None:
        self.redis = redis
        self.clock = clock
        self.lease_s = lease_s
        self.max_waiting = max_waiting
        names = ("running", "waiting", "leases", "backend", "arrivals")
        self.keys = [key_prefix + k for k in names]
        self._join = redis.register_script(JOIN)
        self._try_start = redis.register_script(TRY_START)
        self._finish = redis.register_script(FINISH)
        self._renew = redis.register_script(RENEW)
        self._state = redis.register_script(STATE)

    @staticmethod
    def member(tenant: str, request_id: str) -> str:
        return f"{tenant} {request_id}"

    async def join(self, tenant: str, request_id: str) -> Joined:
        now = self.clock()
        member = self.member(tenant, request_id)
        result = await self._join(
            keys=self.keys, args=[now, member, now + self.lease_s, self.max_waiting]
        )
        accepted, waiting, waiting_total, running = (int(x) for x in result)
        return Joined(bool(accepted), waiting, waiting_total, running)

    async def try_start(
        self, tenant: str, request_id: str, backends: list[tuple[str, int, int]]
    ) -> Turn:
        """backends: the ones that are up, as (name, slots, priority); lower priority first."""
        now = self.clock()
        args: list[object] = [now, self.member(tenant, request_id), now + self.lease_s]
        for name, slots, priority in backends:
            args += [name, slots, priority]
        outcome, backend = await self._try_start(keys=self.keys, args=args)
        outcome, backend = _text(outcome), _text(backend)
        return Turn(outcome, backend or None)

    async def finish(self, tenant: str, request_id: str) -> None:
        """Leave, whether waiting or running. Safe to call more than once."""
        await self._finish(keys=self.keys[:4], args=[self.member(tenant, request_id)])

    async def renew(self, held: list[tuple[str, str]]) -> None:
        if held:
            members = [self.member(t, r) for t, r in held]
            await self._renew(keys=[self.keys[2]], args=[self.clock() + self.lease_s, *members])

    async def state(self) -> QueueState:
        running, waiting, backends = await self._state(keys=self.keys[:4], args=[self.clock()])

        def per_tenant(members: list) -> dict[str, int]:
            return dict(Counter(_text(m).split(" ", 1)[0] for m in members))

        return QueueState(
            per_tenant(running), per_tenant(waiting), dict(Counter(_text(b) for b in backends))
        )
