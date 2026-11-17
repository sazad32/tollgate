"""One Redis round trip before a local request's first token (ADR 0021).

A local request used to make three: reserve its estimate in the tenant's token bucket (limits.py),
join the fair queue, and try to take a free slot (queue.py). ADMIT does all three in one Lua
script, which Redis runs atomically. A request that finds no free slot stays queued, with its
reservation, and waits its turn as before (admission.py).

Redis runs on one node; half the gateways reach it across the link between the nodes, where a
round trip takes ~4 ms and sometimes tens. Week 15 measured 25-105 ms at p99 for the rate limit
and the queue each, before the first token (bench/RESULTS.md).

.NET: like folding three stored-procedure calls into one that calls the other three, inside one
transaction. ADMIT is built from the same Lua functions as RESERVE, JOIN and TRY_START, so the logic
exists once.
"""

from dataclasses import dataclass

from tollgate_gateway.limits import BUCKET_LUA, Decision, Limit, TokenBuckets
from tollgate_gateway.queue import QUEUE_LUA, FairQueue, Joined, _text

# KEYS: the tenant's bucket, then the queue's five keys (running, waiting, leases, backend,
#       arrivals).
# ARGV: now, capacity, refill per second, cost, member, lease expiry, max waiting per tenant, then
#       the backends that are up as triples: name, slots, priority.
# -> {outcome, level after, seconds to wait, this tenant's waiting, everyone's waiting, running,
#     backend}; outcome: start | wait | limited | queue_full
ADMIT = (
    BUCKET_LUA
    + QUEUE_LUA
    + """
local now = tonumber(ARGV[1])
local capacity, rate, cost = tonumber(ARGV[2]), tonumber(ARGV[3]), tonumber(ARGV[4])
local member, expiry, max_waiting = ARGV[5], ARGV[6], tonumber(ARGV[7])
local bucket, q = KEYS[1], queue_keys(2)

-- 1. The token bucket: take the estimate, or turn the request away. Nothing else changes.
local allowed, tokens, wait = take(bucket, capacity, rate, now, cost)
if allowed == 0 then
  return {'limited', tostring(tokens), tostring(wait), 0, 0, 0, ''}
end

-- 2. The fair queue: join, or put the estimate back if this tenant has too many waiting already.
prune(q, now)
local joined = join(q, member, expiry, max_waiting)
if joined[1] == 0 then
  tokens = give(bucket, capacity, rate, now, cost)
  return {'queue_full', tostring(tokens), '0', joined[2], joined[3], joined[4], ''}
end

-- 3. A free slot, if it's this request's turn. If not, it waits and polls TRY_START.
local turn = try_start(q, ARGV[1], member, expiry, backends_from(8))
return {turn[1], tostring(tokens), '0', joined[2], joined[3], joined[4], turn[2]}
"""
)


@dataclass(frozen=True)
class FirstTrip:
    outcome: str  # start | wait | limited | queue_full
    decision: Decision  # the bucket's answer; for queue_full, after the estimate went back in
    backend: str | None = None  # with "start": the backend its slot is on
    joined: Joined | None = None  # the queue's counts; None when limited (it never joined)


class OneTrip:
    """RESERVE, JOIN and TRY_START as one script, on the buckets' and the queue's shared Redis."""

    def __init__(self, buckets: TokenBuckets, queue: FairQueue) -> None:
        self.buckets = buckets
        self.queue = queue
        self._admit = queue.redis.register_script(ADMIT)

    async def admit(
        self,
        tenant: str,
        request_id: str,
        limit: Limit,
        tokens: int,
        backends: list[tuple[str, int, int]],
    ) -> FirstTrip:
        """Reserve `tokens`, join the queue and start if a slot is free and it's this request's
        turn. backends: the ones that are up, as (name, slots, priority)."""
        now = self.queue.clock()
        # Each value encoded exactly as RESERVE, JOIN and TRY_START get it (test_admit.py).
        args: list[object] = [
            now,
            str(limit.capacity),
            repr(limit.refill_per_s),
            repr(tokens),
            self.queue.member(tenant, request_id),
            now + self.queue.lease_s,
            self.queue.max_waiting,
        ]
        for name, slots, priority in backends:
            args += [name, slots, priority]
        keys = [self.buckets.key_prefix + tenant, *self.queue.keys]
        outcome, level, wait, waiting, waiting_total, running, backend = await self._admit(
            keys=keys, args=args
        )
        outcome = _text(outcome)
        decision = Decision(outcome != "limited", float(level), float(wait), limit)
        if outcome == "limited":
            return FirstTrip(outcome, decision)
        joined = Joined(outcome != "queue_full", int(waiting), int(waiting_total), int(running))
        return FirstTrip(outcome, decision, _text(backend) or None, joined)
