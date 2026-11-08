# 0012 — Token rate limits: a token bucket per tenant, reserve then settle

- Status: accepted
- Date: 2026-11-07

## Context
Tenants share a few small GPUs (bench/RESULTS.md: a node serves 2 requests at a time). A tenant's
share has to be limited in tokens, because that is what a request costs, but a request's output length
isn't known until its stream ends (PLAN's hard problem #2). The limit must hold across two gateway
replicas, must not leak capacity when a request is cut short or a pod dies, and must not take the API
down with it.

## Options considered
1. **Algorithm:** a fixed window per minute (simple, but allows double bursts around the boundary), a
   sliding log (exact, but stores every request), or a token bucket (two numbers per tenant, smooth).
2. **Unknown output length:** charge after the fact (a tenant can exceed its limit by any amount
   before anything stops it), reserve the worst case (`max_tokens`, or the model's whole context if
   unset), or reserve an estimate and settle the difference when the request ends.
3. **Shared state:** an in-process limiter per replica (each replica would allow the full limit), or
   Redis, updated with WATCH/MULTI transactions or with Lua scripts.
4. **Redis outage:** fail closed (no service) or fail open (no limits).

## Decision
- **A token bucket per tenant in Redis**, holding one minute's tokens (`capacity` = tokens per minute)
  and refilling continuously. Every change is a Lua script, which Redis runs atomically, so no locks.
  Limits are set per tenant tier (`TOLLGATE_RATE_LIMITS`); the standard tier is 10,000 per minute.
- **Reserve, then settle.** Before forwarding, reserve an estimate: prompt characters ÷ 3, plus 100
  for the chat template, plus `max_tokens` (512 if unset). Refused reservations get an OpenAI-style 429
  with `Retry-After`. When the request ends, settle exactly once:
  - with the model server's usage report (the gateway always requests it on streams, and strips that
    chunk again for clients who didn't ask);
  - if the client left before the report: prompt estimate plus one token per chunk sent;
  - if the model server failed or refused: a full refund.
  If the real count is above the estimate, the excess is still charged and the bucket goes negative:
  debt, paid off by waiting.
- **Requests larger than the whole bucket** are admitted only when it's full, and leave it in debt.
  Otherwise they could never be admitted.
- **The gateway's clock** is passed into the scripts, which never move a bucket's time backwards (two
  replicas on two nodes). Idle buckets expire from Redis exactly when they'd be full again, so a
  missing bucket means a full one.
- **Fail open.** If Redis is unreachable (0.5 s timeouts), requests go through without limits and this
  is logged. Limits protect capacity, not money: billing (week 10) still counts every token. Auth is the
  opposite (ADR 0010), because a skipped check there lets strangers in.

## Consequences
- **Verified in the cluster** at 400 tokens per minute:
  - a stream in flight held its 309-token reservation;
  - a concurrent request from the same tenant got 429 with `Retry-After: 30`;
  - the other tenant got through at the same moment;
  - after settling, the bucket matched the formula to within timing;
  - with Redis stopped, requests were served without limit headers.
- **Crash safety without cleanup:** a reservation lost to a crashed pod is simply never refunded. The
  tenant overpays once and the bucket refills within a minute; there are no in-flight records to leak.
- **Estimates hold back capacity while a request runs.** A tenant with long `max_tokens` values gets
  fewer concurrent requests than its real usage would allow, until each settles.
- **Undercounting on vLLM:** the chunk count undercounts when a client disconnects on vLLM, which can
  pack several tokens per chunk. Billing must not rely on it (hard problem #1, week 11).
- **Limits are only as strict as Redis is up:** an outage means unlimited usage for its duration.
  Visible in the logs; alerting comes later.
- Per-tenant queues and concurrency limits (week 9) are a separate layer on top.
