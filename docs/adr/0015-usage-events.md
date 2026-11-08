# 0015 — Usage events: one per request a model ran, to Redis Streams, at least once

- Status: accepted
- Date: 2026-11-08

## Context
Phase 3 bills each tenant for exactly what it used. The gateway is the only component that sees
every request end, so it must record usage. But billing must never sit in the request path (ADR
0002): a billing outage can't take the API down. Two hard problems meet here:
- **#1, a stream that dies halfway:** the model's own token count arrives in the stream's last
  chunk, which never comes if the client leaves.
- **#3, exactly-once usage events** across retries and restarts.

Redis already runs in the cluster, but with no persistence, so it couldn't keep a queue of money.

## Options considered
1. **Where events go:** straight into Postgres from the gateway (billing's database in the request
   path), Kafka (a new, heavy dependency), or Redis Streams (in the stack, already running).
2. **How the request hands them over:** write to Redis before answering (Redis latency and outages
   in the request path), or append to memory and write in the background.
3. **Delivery:** at most once (never duplicated, sometimes lost), or at least once (never lost while
   the pod lives, sometimes duplicated) with deduplication by an idempotency key at the consumer.
4. **When Redis is down:** drop events, spool them to disk (the gateway's root filesystem is
   read-only), or hold them in a bounded in-memory buffer and retry.

## Decision
- **One event per request a model ran**, in the Redis stream `tollgate:usage`, in the format of
  docs/usage-events.md (schema version 1).
  - It carries `idempotency_key = <request_id>:usage`. The gateway makes the request ID, so a retry
    inside the gateway keeps the key, while a client sending again is a new request.
  - It names the model that actually answered, so overflows and refusal fallbacks are priced right.
  - No event when no model ran: 401/403/429, unreachable backends, backend errors.
- **Every request ends exactly once**, in `RequestUsage`:
  - `record()` when a model ran: it settles the token bucket (ADR 0012) and emits the event, with the
    same numbers.
  - `refund()` when none did.
- **Tokens are `reported` or `estimated`.** `reported` comes from the model's usage report (always
  requested on streams). `estimated` is used when the client left or the stream broke first: the
  prompt estimate, plus one token per chunk sent. The status says how the request ended:
  `completed`, `client_disconnected` or `failed`.
- **The request only appends to memory.** A background loop writes to the stream in batches. If
  Redis is down, events wait (up to 10,000 per replica) and the loop retries every 5 s. An event
  that doesn't fit is logged in full at ERROR, to be replayed by hand. On shutdown, the gateway
  makes one last attempt to flush.
- **At least once:** a retry can write an event twice. Week 11's ledger makes it exactly once, with
  the idempotency key as its primary key.
- **Redis keeps the stream:**
  - It becomes a StatefulSet on node-2, with an append-only file fsynced every second, on a static
    local volume like Postgres (ADR 0006).
  - Its memory cap goes from 64 to 256 MB. The stream has no expiry, so `volatile-lru` never evicts
    it: when Redis is full, writes fail (and wait in the gateway) instead of losing events.

## Consequences
- **Verified in the cluster:**
  - Plain, streamed and hosted requests each gave one event, matching the usage the client saw
    (74/7, 74/60, Claude 16/16).
  - A client that hung up after reading 5 chunks gave `estimated` / `client_disconnected`, 108
    in and 19 out: the gateway had sent 19 chunks into the port-forward's buffers, and the model had
    generated them.
  - A 403 gave none.
  - Restarting Redis kept every event.
  - With Redis stopped, a request still got 200, and its event arrived within seconds of Redis
    coming back.
- **Estimates are approximate:** the prompt estimate overcounts SmolLM3 (108 vs 74 above), and one
  token per chunk undercounts vLLM, which packs several. The event says which numbers are estimates;
  reconciliation (week 13) compares them with what the backends report.
- **One way to lose events:** a gateway pod that crashes while Redis is down loses its buffer.
  Normally the buffer holds milliseconds' worth.
- **The append-only file can lose its last second** if node-2 crashes (fsync every second).
- **The stream grows until week 11's consumer** acknowledges and trims it: roughly 400 bytes an
  event, so 256 MB holds about 600,000.
- **Rule 5 holds:** billing can be down for as long as Redis has room; the API doesn't notice.
