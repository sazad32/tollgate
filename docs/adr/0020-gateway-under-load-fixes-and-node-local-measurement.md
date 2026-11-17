# 0020 — The gateway under load: bounded pools, backpressure, cheaper tokens, measured node-local

- Status: accepted (amends ADR 0019's test setup; its targets are unchanged)
- Date: 2026-11-17

## Context
Week 14 (ADR 0019) found where the gateway breaks. Two replicas held 250 streams and failed at
500, against a target of 1,000, and missed the latency targets at every load. The limits, in the
order they hit:
- the upstream connection pool (200 per replica);
- one CPU core per replica;
- redis-py's pool of 100 connections, past which rate limits and the fair queue fail open;
- no backpressure, so a replica ran out of memory.

Week 15 had to fix them, re-run the same steps, and record before and after. Two things turned up
along the way:
1. **Most of the CPU went to plumbing, not to Tollgate's logic.** A profile with py-spy (run only
   in a throwaway Linux container on the workstation, not added to the project) split one
   replica's CPU per token like this:
   - reading from the model: 43%, of which anyio's timeout wrapper took 12.5% and the h11 parser
     about 9%;
   - writing to the client: 18%;
   - the event loop itself: 16%;
   - the stream meter: 8%.
2. **The setup measured the network as the gateway's.** Week 14 put the clients and the fake
   server on node-1 and the gateway on node-2. Through the gateway, every token crossed between
   the nodes twice; straight to the fake server, never. That link turned out to be slow and
   jittery: a TCP round trip takes about 4 ms at the median, 5–13 ms at p90 and up to 36 ms,
   against 0.3 ms within a node. Run all on node-2, the same image added 2 ms to p99 token gaps
   at 100 streams, instead of 24 ms across the nodes.

## Options considered
1. **The upstream pool.**
   - One bigger pool. httpcore walks every idle connection on each request, so its cost grows
     with the square of the pool's size.
   - **Several small pools**, taken in turn.
2. **The Redis pool.**
   - redis-py's default: 100 connections, and an error at once when they're all busy.
   - **A blocking pool:** more connections, and a short wait for a free one.
3. **Backpressure.**
   - None: memory grows until the pod is killed, and every stream on it drops.
   - Rely on the fair queue: it holds only local requests, and needs Redis.
   - **A cap on requests in flight per replica:** past it, a quick 503 with Retry-After.
4. **CPU per token.**
   - **uvloop and httptools** for uvicorn: an event loop and an HTTP parser written in C.
   - **A plain asyncio socket layer** under httpx, in place of anyio's (the timeout wrapper is
     the cost).
   - **A shortcut in the stream meter** for the common case.
   - Our own HTTP/1.1 client on httptools: about 25% less CPU again in a local test, but we'd
     own every HTTP corner case (chunked bodies, keep-alive, errors).
   - orjson for JSON: a new dependency, for a small share.
   - Python 3.13 or later: a stack change.
5. **How to measure.**
   - As in week 14, which counts the slow link as the gateway's.
   - **Node-local:** each request stays on one node, from client to gateway to fake server. The
     link is reported on its own.

## Decision
- **The gateway:**
  - **16 small upstream pools** (`TOLLGATE_UPSTREAM_POOLS`), each with a share of the
    connections. The TLS context is built once and shared.
  - **A backend is marked down only after 3 load polls in a row time out,** not after one: a busy
    gateway's poll can be late while the backend is fine.
  - **A blocking Redis pool:** 500 connections, a wait of up to 0.5 s for a free one
    (`TOLLGATE_REDIS_MAX_CONNECTIONS`, `TOLLGATE_REDIS_POOL_TIMEOUT_S`). Failing open still
    happens when Redis is down, but now it's counted, by step:
    `tollgate_gateway_redis_fail_open_total{step}`. A queue slot whose release fails is retried,
    so it doesn't stay taken until its 30 s lease expires.
  - **Backpressure:** `TOLLGATE_MAX_IN_FLIGHT` caps the model requests one replica serves at once.
    Past it, a quick 503 with Retry-After. Unset means no cap.
  - **uvicorn on uvloop and httptools,** with a 65 s keep-alive. That outlasts the clients' own idle
    timeout, so the client closes an idle connection first; it never reuses one the server is
    closing.
  - **Backend URLs fully qualified, with a trailing dot** (`...svc.cluster.local.`): one DNS query
    per new connection instead of one per search domain (about 5 ms instead of 15).
  - **512 MiB memory limit** per replica.
  - **`netio.py`:** httpcore's network layer rebuilt on plain asyncio streams, with
    `asyncio.timeout` instead of anyio's wrapper, and `TCP_NODELAY`. httpx and httpcore still do
    all the HTTP. It plugs into httpx's private `_pool` attribute (httpx 0.28), so
    `test_netio.py` checks that every request really goes through it, plus timeouts, errors,
    keep-alive reuse, and that a connection the server closed isn't reused.
  - **The stream meter's shortcut:** when a read is exactly one whole event, it skips the general
    parser. `test_stream_meter.py` checks it against a frozen copy of the old meter, over 4,000
    generated streams.
- **The test setup (amends ADR 0019):**
  - a fake model server on each node (one pod per node);
  - the load-test gateway as 7 single-process pods, 5 on node-1 and 2 on node-2. node-2 has 4
    cores and also runs Redis and Postgres.
  - Services with `internalTrafficPolicy: Local`, so a client reaches the gateway on its own node,
    and that gateway the fake server on its own node;
  - client pods on each node, 5 and 2 (`load-job --nodes`), sharing one load;
  - steps of 250, 500, 750 and 1,000 streams;
  - a cheaper setup: clients on uvloop, and the fake server on uvloop and httptools. That cut the
    setup's own CPU at 1,000 streams from 5.6 cores to 4.6.
- **The targets don't change.** "Adds" is still the gateway's p99 minus the fake server's, with the
  same clients, at the same step.

## Consequences
- **Before and after** (bench/RESULTS.md has the tables):
  - **CPU per token:** about 20% less, from netio and the meter shortcut (a local A/B test, medians
    191 → 151 µs per chunk).
  - **Same setup as week 14, 2 replicas** (before netio and the meter shortcut):
    - 250 streams: the gateway adds 130 ms to p99 first token (week 14: 200) and 29 ms to p99
      token gaps (week 14: 37).
    - 500 streams: 3 errors out of 1,757 (week 14: 18), and a p99 first token of 0.7 s
      (week 14: 112 s).
    - 1,000 streams: at 300 per replica, the cap turns the rest away with quick 503s. In week 14,
      replicas ran out of memory instead.
  - **Node-local, 7 gateway processes, two runs:**
    - token gaps: within target at 250 and 500 streams in both runs (+3 to +4 ms), and at 750 in
      one (+7 ms; the other: +22 ms);
    - first token: within target only once, at 500 streams in the first run (+30 ms). Otherwise
      it adds 100–190 ms up to 750 streams, and 0.7–1.3 s at 1,000;
    - errors: one 504 in about 17,500 requests across both runs.
- **The Phase 4 gate is not met.** Two things stand in the way:
  - **The first token waits for 3 Redis round trips:** reserve tokens (rate limit), join the fair
    queue, take a slot. Redis is on node-2, so for node-1's gateways (70% of the traffic) each one
    crosses the slow link. At p99, the rate limit and the queue each take about 25–105 ms even at
    250–750 streams.
  - **1,000 streams don't fit this cluster beside the test setup.** In the cluster, one gateway
    process holds the targets up to about 100 streams, at about 0.45 cores. 1,000 streams need
    about 10 of those (4.5 cores), on top of the setup's 4.6. At 1,000, node-1 ran 7.1 of its 8
    CPUs busy and the gateways' event loops lagged 220–330 ms.
- **What got harder:**
  - netio is our own code under httpx. An httpx upgrade has to pass `test_netio.py`, and a change
    to the private `_pool` would break it.
  - The meter has two paths. The differential test keeps them equal.
- **Still open** (weeks 16–17 planning; the gate is judged at the end of week 17):
  1. **Cut the Redis round trips before the first token from 3 to 1:** one Lua script that
     reserves, joins and takes a free slot. It's core rate-limit and queue logic, so the design
     comes first, then tests.
  2. **The link between the nodes.** That's hardware, not code.
  3. **How production scales.** ADR 0019's target says "the 2 replicas", but one process holds
     about 100–250 streams. The choice is between more single-process pods and several uvicorn
     workers per pod. Production sets no in-flight cap until then.
  4. **Occasional 504s:** one in the second node-local run, and 17 in one 50-stream step of a
     repeat run on node-2. Not explained yet.
  5. **Billing's backlog** wasn't tracked in this week's runs. It held in week 14.
