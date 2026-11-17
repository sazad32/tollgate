"""Token rate limits: a token bucket per tenant in Redis; reserve first, settle later (ADR 0012).

.NET: the algorithm of System.Threading.RateLimiting.TokenBucketRateLimiter.
Gotcha: two gateway replicas share each bucket, so it lives in Redis, and every change is one Lua
script. Redis runs a script start to finish with nothing in between: no lock, no race.

The bucket holds up to `capacity` tokens (one minute's worth) and refills at capacity/60 per
second. A request reserves an estimate before it's forwarded, and is settled with the real count
when it ends: the difference is refunded, or charged as debt (the bucket goes negative) if low.
"""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from redis.asyncio import Redis

DEFAULT_MAX_TOKENS = 512  # assumed output when a request doesn't set max_tokens
TEMPLATE_ALLOWANCE = 100  # tokens the chat template adds around the messages
CHARS_PER_TOKEN = 3  # deliberately generous (English is nearer 4); settlement refunds the rest

# The bucket, as Lua functions: shared by RESERVE and SETTLE below, and by ADMIT (admit.py), which
# reserves and joins the fair queue in one call. They take the bucket's key and numbers rather
# than reading KEYS and ARGV, so each script passes its own.
BUCKET_LUA = """
-- Bring the bucket up to date -> tokens, ts. A missing bucket is a full one (idle buckets expire
-- from Redis exactly when they'd be full again). Time only moves forward: if this caller's clock is
-- behind the last writer's (two replicas, two nodes), nothing is refilled and `ts` stays.
local function refill(bucket, capacity, rate, now)
  local state = redis.call('HMGET', bucket, 'tokens', 'ts')
  local tokens, ts = tonumber(state[1]), tonumber(state[2])
  if tokens == nil then
    tokens = capacity
    ts = now
  end
  if now > ts then
    tokens = math.min(capacity, tokens + (now - ts) * rate)
    ts = now
  end
  return tokens, ts
end

-- Store the level, and let the key expire when the bucket would be full again (absent == full).
local function save(bucket, capacity, rate, tokens, ts)
  redis.call('HSET', bucket, 'tokens', tostring(tokens), 'ts', tostring(ts))
  redis.call('EXPIRE', bucket, math.ceil((capacity - tokens) / rate) + 1)
end

-- Take `cost` if the bucket holds enough -> allowed (0/1), level after, seconds to wait.
-- A request costing more than the whole bucket is admitted only when the bucket is full, and
-- leaves it in debt; otherwise it could never be admitted at all.
local function take(bucket, capacity, rate, now, cost)
  local tokens, ts = refill(bucket, capacity, rate, now)
  local needed = math.min(cost, capacity)
  local allowed, wait = 0, 0
  if tokens >= needed then
    tokens = tokens - cost
    allowed = 1
  else
    wait = (needed - tokens) / rate
  end
  save(bucket, capacity, rate, tokens, ts)
  return allowed, tokens, wait
end

-- Add `delta`: a refund if positive, a charge if negative -> level after.
local function give(bucket, capacity, rate, now, delta)
  local tokens, ts = refill(bucket, capacity, rate, now)
  tokens = math.min(capacity, tokens + delta)
  save(bucket, capacity, rate, tokens, ts)
  return tokens
end
"""

# KEYS: the bucket. ARGV: capacity, refill per second, now, cost.
# -> {allowed, level after, seconds to wait}
RESERVE = (
    BUCKET_LUA
    + """
local allowed, tokens, wait = take(KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2]),
                                   tonumber(ARGV[3]), tonumber(ARGV[4]))
return {allowed, tostring(tokens), tostring(wait)}
"""
)

# KEYS: the bucket. ARGV: capacity, refill per second, now, delta (reserved - actual: a refund if
# positive, a charge if negative). -> the level after
SETTLE = (
    BUCKET_LUA
    + """
return tostring(give(KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3]),
                     tonumber(ARGV[4])))
"""
)


@dataclass(frozen=True)
class Limit:
    tokens_per_minute: int

    @property
    def capacity(self) -> int:
        return self.tokens_per_minute

    @property
    def refill_per_s(self) -> float:
        return self.tokens_per_minute / 60


@dataclass(frozen=True)
class Decision:
    allowed: bool
    remaining: float  # tokens left in the bucket after this decision; negative is debt
    retry_after_s: float  # 0 when allowed
    limit: Limit

    @property
    def seconds_to_full(self) -> float:
        return max(0.0, (self.limit.capacity - self.remaining) / self.limit.refill_per_s)


def _characters(messages: object) -> int:
    total = 0
    for message in messages if isinstance(messages, list) else []:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):  # content parts: [{"type": "text", "text": "..."}, ...]
            total += sum(
                len(part["text"])
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
    return total


def prompt_estimate(body: dict) -> int:
    """The prompt part of the estimate alone: characters / CHARS_PER_TOKEN + TEMPLATE_ALLOWANCE."""
    return math.ceil(_characters(body.get("messages")) / CHARS_PER_TOKEN) + TEMPLATE_ALLOWANCE


def estimate_tokens(body: dict) -> int:
    """Rough upper estimate of a request's total tokens: prompt (from characters) + max output."""
    output = body.get("max_tokens") or body.get("max_completion_tokens") or DEFAULT_MAX_TOKENS
    return prompt_estimate(body) + int(output)


class TokenBuckets:
    """All tenants' buckets in one Redis. `clock` returns seconds; tests pass a fake one."""

    def __init__(
        self,
        redis: Redis,
        clock: Callable[[], float] = time.time,
        key_prefix: str = "tollgate:bucket:",
    ):
        self.redis = redis
        self.clock = clock
        self.key_prefix = key_prefix
        self._reserve = redis.register_script(RESERVE)
        self._settle = redis.register_script(SETTLE)

    def _args(self, limit: Limit, amount: float) -> list[str]:
        return [str(limit.capacity), repr(limit.refill_per_s), repr(self.clock()), repr(amount)]

    async def reserve(self, tenant_id: str, limit: Limit, tokens: int) -> Decision:
        """Take `tokens` from the tenant's bucket if it holds enough; otherwise take nothing."""
        allowed, level, wait = await self._reserve(
            keys=[self.key_prefix + tenant_id], args=self._args(limit, tokens)
        )
        return Decision(bool(int(allowed)), float(level), float(wait), limit)

    async def settle(self, tenant_id: str, limit: Limit, reserved: int, actual: int) -> float:
        """Correct a reservation with the real count: refund reserved - actual, or charge the
        excess. Returns the bucket's level afterwards."""
        level = await self._settle(
            keys=[self.key_prefix + tenant_id], args=self._args(limit, reserved - actual)
        )
        return float(level)

    async def level(self, tenant_id: str, limit: Limit) -> float:
        """The bucket's current level, refilled to now (read-only; for headers and tests)."""
        tokens, ts = await self.redis.hmget(self.key_prefix + tenant_id, ["tokens", "ts"])
        if tokens is None:
            return float(limit.capacity)
        elapsed = max(0.0, self.clock() - float(ts))
        return min(float(limit.capacity), float(tokens) + elapsed * limit.refill_per_s)
