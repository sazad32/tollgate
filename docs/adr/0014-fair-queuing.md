# 0014 — Fair queuing: requests wait in the gateway, and free slots go to the tenant with fewest running

- Status: accepted
- Date: 2026-11-07

## Context
The two nodes serve 4 requests at a time (bench/RESULTS.md). Until now, the gateway forwarded every
local request at once, and the overflow waited in llama-server's own first-come queue. A tenant that
sent 12 requests made everyone else wait behind all 12 (PLAN's hard problem #4: fairness when the GPU
is saturated). Token limits (ADR 0012) cap how much a tenant uses per minute, not how many requests it
has in flight at one moment. The fix must hold across both gateway replicas, survive a replica dying
mid-request, and not take the API down with it.

## Options considered
1. **Where requests wait:** in the backends' queues (first come, first served, no tenant in sight), in
   each gateway replica's memory (each sees half the traffic), or in Redis (one queue for both).
2. **Who goes next:** first come, first served; round robin between tenants; weighted by tokens
   (deficit round robin); or "the tenant with the fewest requests running".
3. **Which backend:** pick it from the polled `/metrics` (up to a second old), or let the queue hand
   out a slot on a specific backend.
4. **Crash safety:** counters (leak if a replica dies), or leases that must be renewed.
5. **When to stop waiting:** never, or after a bounded queue length or wait, with a 429 and a hint.

## Decision
- **Requests wait in the gateway, in Redis.** A local request goes to a backend only when it holds a
  slot. Both replicas share one queue, and every change is a Lua script, as in ADR 0012.
- **The rule:** free slots are handed out one at a time. Each goes to the waiting tenant with the
  fewest requests running; ties go to whoever's next request has waited longest; within a tenant,
  it's first come, first served. Nothing is wasted: a tenant on its own may use every slot.
- **A slot is on a specific backend.** The queue records which backend each running request holds and
  gives a new request a slot on a backend with room: the preferred kind first (vLLM), then the most
  room. `/metrics` still decides which backends are up and how many slots they offer.
- **Leases.** Every waiting or running request holds a 30 s lease that its replica renews every 10 s.
  A dead replica's slots come back by themselves. A replica stops renewing any slot held for more
  than 30 min, a backstop against a release that never ran.
- **Bounded waiting:** at most 16 waiting per tenant, and at most 120 s of waiting. Both answer 429
  with `Retry-After`, estimated as (requests waiting + 1) × average slot hold time ÷ slots. A client
  that hangs up while waiting leaves the queue.
- **Arrival order** comes from a Redis counter, not a timestamp: the two replicas' clocks differ.
- **Unchanged:** `hosted_allowed` tenants still overflow to the hosted model instead of waiting
  (ADR 0013). If Redis is down, requests go ahead without queuing (fail open, as with rate limits).
  `TOLLGATE_FAIR_QUEUE=false` turns queuing off.

## Consequences
- **Measured** (bench/RESULTS.md, two tenants on the two nodes): behind a 12-request burst from
  `support`, `billing`'s median wait for a first token fell from 32.4 s to 13.8 s, and its worst from
  40.6 s to 18.0 s. Total throughput didn't change (all 17 requests done in 62–64 s either way). The
  burst and the trickle came through different replicas.
- **The first version chose the backend from `/metrics`** and sent admitted requests to a node that
  was still full, where they waited a whole generation (16.7 s median, against 13.8 s after the fix).
- **Fair by slots, not by tokens.** A tenant with long requests holds slots longer. Token limits
  (ADR 0012) bound the volume; a token-weighted queue can come later if tenants' request sizes differ
  a lot.
- **Not preemptive.** A slot frees only when a generation ends, so the best case is still "wait for
  the next request to finish": seconds on vLLM, about 10 s on a node.
- **The queue counts only gateway traffic.** Load sent straight to a model server (benchmark Jobs)
  is invisible to it. Every client goes through the gateway, so this is accepted.
- **Polling:** each waiting request asks Redis every 100 ms (16 waiting × 2 tenants = ~320 small
  scripts a second at most). Pub/sub wake-ups can replace this if it ever matters.
- **Overflow is decided before queuing**, from `/metrics`. A `hosted_allowed` request that sees free
  slots there but then finds the queue full waits in it rather than overflowing.
