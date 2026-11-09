# 0019 — Load testing the gateway: a fake model server, an open-loop client, targets set first

- Status: accepted
- Date: 2026-11-09

## Context
Phase 4's gate is "latency targets hold under load". No targets existed, and nothing had measured
the gateway itself under load:
- The GPUs serve about 4 streams at once (36 with vLLM). They hide the gateway completely.
- The hosted model takes overflow, and that can mean far more concurrent streams than any GPU.
- Every stream passes through one Python process per gateway replica. A single asyncio event loop
  and a single CPU core per pod could be the limit (ADR 0001 expected trouble here: blocking
  calls, backpressure, pool limits).

## Options considered
1. **What the gateway sends to:**
   - the real model servers: GPU-bound, so they hide the gateway;
   - **a fake model server** that streams tokens at a set pace, with no GPU.
2. **How load is applied:**
   - a closed loop: N users, each waiting for its last answer. It sends less as the gateway slows
     down, which hides the slowdown ("coordinated omission").
   - **an open loop:** streams started at a fixed rate, in steps.
3. **The load generator:**
   - Locust or k6: new dependencies, and another language;
   - **the bench client, extended** (asyncio + httpx, ADR 0008). It is first measured on its own
     against the fake server, to prove it isn't the limit.
4. **Which gateway:**
   - the production one;
   - **a second gateway Deployment** with the same image, Redis and Postgres, and the fake server
     as its only backend.
5. **Seeing inside the gateway:**
   - logs;
   - **Prometheus metrics in the gateway** (`prometheus-client`): stage timings and event-loop
     lag;
   - a profiler such as py-spy, a new tool. Deferred until the metrics run out.
6. **The targets:** set after measuring, or **before measuring**, so the results can't move them.

## Decision
- **The targets.** These are the Phase 4 gate. At **1,000 concurrent streams** across the 2
  gateway replicas, each 256 tokens at 20 tokens a second:
  - the gateway adds **at most 50 ms to time to first token, at p99**;
  - it adds **at most 10 ms to the gap between tokens, at p99**;
  - **no errors**, other than deliberate 429s. With the fake server, slots never run out, so any
    429 counts as an error;
  - **billing keeps up:** the stream's backlog is back to 0 within a minute of the run ending.

  "Adds" means the percentile through the gateway minus the same percentile straight to the fake
  server, with the same clients and the same step.
- **The fake model server** is `tollgate_bench mock`.
  - It is a bare ASGI app under 4 uvicorn workers, and OpenAI-compatible.
  - It answers with exactly `max_tokens` tokens: the first after 100 ms, the rest at 20 a second
    on a fixed schedule, then a usage report.
  - Its `/metrics` look like llama-server's, so the gateway routes to it like any backend.
- **The load generator** is `tollgate_bench load`.
  - Each step starts streams at the rate that keeps the wanted number open: rate = streams ÷
    stream length (Little's law).
  - Time to first token, every gap between tokens, and each stream's duration go into
    fixed-bucket histograms (1 ms wide under 100 ms), which merge across client pods.
  - The client records its own late starts and event-loop lag, so a saturated client can't pass
    for a slow gateway.
  - `load-job` runs it as an Indexed Job, every pod starting at the same moment, at about **150
    streams per client pod**. That figure was measured: one client process fills one core at
    around 250–300 streams of 20 tokens a second.
- **The test setup**, `deploy/loadtest`, applied by hand and deleted after:
  - the fake server, on node-1 with the clients;
  - `tollgate-gateway-load` on node-2, with the production gateway's resources. Its backend is the
    fake server with 5,000 slots, so the fair queue still runs in Redis on every request but never
    fills.
  - A `loadtest` tenant, whose tier allows 10⁹ tokens a minute, with its key in the hand-made
    `loadtest-key` Secret.
  - Usage flows into the real stream and ledger as model `mock-model`, priced at 0.00, so
    billing's throughput is part of the test.
- **Gateway metrics at `/metrics`,** scraped by a PodMonitor for every gateway pod:
  - requests in flight, by status;
  - time to the first and the last byte;
  - the time each step before the model takes: `auth`, `rate_limit`, `queue`,
    `upstream_headers`;
  - event-loop lag: how late a 250 ms sleep wakes up;
  - usage events buffered.

  `TOLLGATE_LOOP_DEBUG=true` turns on asyncio's slow-callback warnings for a run.

## Consequences
- **Where it breaks today** (2026-11-09; bench/RESULTS.md has the tables):
  - **One replica** carries about 100 streams. At 250 it drops connections and first tokens take
    seconds. At 1,000 it runs out of memory (the 256 MiB limit) and restarts.
  - **Two replicas** carry 250 with no errors, and fail at 500.
  - **Even at 100 to 250 streams, the latency targets are missed:** the gateway adds about
    200–440 ms to p99 time to first token, and 16–37 ms to p99 token gaps.
- **The limits, in the order they hit:**
  1. **The upstream connection pool** (`TOLLGATE_UPSTREAM_MAX_CONNECTIONS`, 200 per replica).
     The 201st stream waits for a free connection, up to 10 s, and then fails with a 504.
  2. **One CPU core per replica.** At about 500 streams per replica the single event loop is full,
     at 1.0 core, and lags 0.4–2 s. That lag lands on every step: a cached key check takes
     1.7 s. The load poller's 1 s timeout then marks the healthy backend as down (503s). uvicorn's
     keep-alive timer closes connections just as clients reuse them (dropped connections).
  3. **The Redis connection pool.** redis-py 8 defaults to 100 connections and fails at once
     when they're all busy. The gateway then fails open, as designed, so **rate limits and the
     fair queue silently switch off** under load. It also can't release queue slots, which leak
     until their 30 s lease expires. On a real llama-server with 2 slots, that would block a GPU.
  4. **The fair queue's 16-waiting-per-tenant limit** trips once Redis is slow, giving 429s even
     with 5,000 free slots.
  5. **No backpressure.** Requests in flight grow without bound (1,950 in one pod) until the pod
     runs out of memory and every connection drops at once.
- **Unexplained, for week 15:** even at 100 streams, a few requests wait about 2 s for the model's
  response headers. That's probably the cost of opening new upstream connections; DNS in
  Kubernetes (`ndots`) is the usual suspect.
- **What held:** billing kept up throughout. The stream's backlog stayed at 0, and at most 23
  events were buffered in a gateway. The ledger got exactly one row per request a model ran.
- **Week 15 fixes these and re-runs the same steps,** recording before and after.
- **Caveats:**
  - The clients and the fake server share node-1 with Keycloak and a llama-server. The gateway
    under test shares node-2 with Redis and Postgres.
  - The fake server's tokens are tiny. Real chunks are a little bigger, but the cost per chunk is
    mostly fixed.
  - Each run adds a few thousand `loadtest` rows to the ledger. They're synthetic and priced at
    0.00.
