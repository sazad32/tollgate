# Dev log

Newest entry at the top. Each entry: what changed, what broke, what was measured.

## 2026-11-08 — Week 11: billing consumer and usage ledger
- Changed: billing reads `tollgate:usage` as the consumer group `billing` and writes each event
  once to `usage_ledger`. The ledger is keyed by the idempotency key, and billing acknowledges only
  after the commit. Bad entries are set aside. Hang-ups are billed at the estimate and failures at
  0 (ADR 0016).
- Broke: fakeredis answers blocking reads instantly, which starved the event loop in tests.
- Measured: all 358 tests pass. On the cluster, a duplicate, a full replay and a 25 s Postgres
  outage changed nothing: 13 keys in the stream, 13 rows in the ledger.

## 2026-11-08 — Week 10: usage events
- Changed: every request a model ran ends in one usage event (idempotency key `<request id>:usage`,
  the model that answered, reported or estimated tokens, how it ended) in the Redis stream
  `tollgate:usage`; the request only appends to memory, a background loop writes, retries while Redis
  is down (ADR 0015). Redis is now a StatefulSet with an append-only file on node-2. 34 tests written
  first.
- Broke: two tests read the emitter's counters a moment before it updated them; a rate-limit test
  needed both requests in flight at once, because the fake model answered too fast to hold a
  reservation.
- Measured: events matched the client's usage (74/7, 74/60, Claude 16/16); a client that read 5
  chunks and hung up was charged 19 estimated output tokens, already sent into the port-forward's
  buffers; events survived a Redis restart, and with Redis stopped a request still got 200 and its
  event arrived within seconds of Redis returning.

## 2026-11-07 — Week 9: fair queuing (Phase 2 gate met)
- Changed: local requests wait in the gateway, in Redis, for a slot on a specific backend; each free
  slot goes to the tenant with the fewest requests running; 429 + Retry-After past 16 waiting or
  120 s; leases free a dead replica's slots (ADR 0014). New `tollgate_bench fairness` demo. 43 new
  tests. Phase 2 gate met.
- Broke: the first demo routed admitted requests by /metrics up to a second old, so some waited a
  whole generation on a node that was still full; the queue now hands out slots per backend.
- Measured: behind a 12-request burst, the other tenant's median wait for a first token fell from
  32.4 s to 13.8 s (worst 40.6 → 18.0 s), with the same total throughput; at 1,200 tokens/min one
  tenant got 429s while the other got 200.

## 2026-11-07 — Week 8: router and data policy
- Changed: the gateway routes by each backend's load, read from its /metrics every second: vLLM first,
  then the node with most room, with failover. Claude, through the anthropic SDK, only for
  hosted_allowed tenants, when asked for by name or when local is full (ADR 0013). 88 tests written first.
- Broke: 5 concurrent streams split 1 and 4 across the nodes (requests were counted only once a backend
  answered; now when sent); a rollout left both gateway replicas on node-2 (the spread rule now counts
  per revision); httpx logged every poll.
- Measured: vLLM took 6 of 6 concurrent requests, and stopping it caused no client errors; Claude
  answered in under 2 s, for a fraction of a cent in all; the SDK adds ~45 MiB per gateway pod.

## 2026-11-07 — Week 7: token rate limits
- Changed: per-tenant token bucket in Redis (atomic Lua); reserve an estimate, settle with real usage;
  429 with Retry-After and x-ratelimit headers; fail open without Redis (ADR 0012); Redis 8 in k3s.
- Broke: my first 429 tests let requests through, because settlement refunded the reservation; they
  now use a reply that really spends the budget.
- Measured: at 400 tokens/min, a stream in flight held 309 tokens; a concurrent request got 429
  (retry in 30 s) while the other tenant got 200.

## 2026-11-07 — Week 6: tenant authentication
- Changed: /v1 now needs a tenant API key (HMAC-hashed) or a Keycloak token (ADR 0010); Postgres 18 and
  Keycloak 26 in k3s (ADR 0011); migration Job, admin CLI; 47 tests written first.
- Broke: "last used" was stamped on revoked keys (fixed); both gateway replicas landed on node-2 (spread
  is now a hard rule); a pasted secrets command split across two lines.
- Measured: cached auth adds nothing measurable, a cache miss 35–170 ms; revocation bites within 60 s;
  Keycloak uses 571 MiB of RAM.

## 2026-11-07 — Week 5: gateway skeleton
- Changed: FastAPI gateway as a pass-through streaming proxy (ADR 0009): OpenAI errors, request IDs,
  one safe retry, disconnects stop the model; image built in CI, 2 replicas in k3s.
- Broke: anyio cancels the cleanup too unless shielded; PowerShell 5.1 treats docker's stderr as
  errors; vLLM's start needs ~2.5 GB free RAM, so the browser had to close.
- Measured: gateway adds 3–7 ms to first token, nothing per token; a disconnect stopped generation
  after 49 of 400 tokens.

## 2026-11-07 — Week 4: benchmark (Phase 1 gate)
- Changed: `tollgate_bench`, an asyncio + httpx benchmark run as an in-cluster Job (ADR 0008); results
  in `bench/RESULTS.md`; Phase 1 gate met, Phase 2 starts.
- Broke: 6 of 936 requests hit a keep-alive race (llama-server and httpx both drop idle connections at
  5 s); low memory on the workstation killed a background waiter.
- Measured: per node ~20 / 17 tok/s, flat from 8 users; vLLM 113 → 1,103 tok/s; the Service balances
  unevenly at 32+ users.

## 2026-11-07 — Week 3: monitoring
- Changed: kube-prometheus-stack via k3s's Helm controller (ADR 0007); DCGM GPU exporter; scrape targets
  for llama-server and vLLM (via a Service with no pods behind it); dashboards: Model servers, GPUs.
- Broke: DCGM exporter OOM-killed at a 384 MiB limit (uses ~420); node-2's uutils `install` rejected a
  numeric group; Grafana 13 is distroless, so no in-container checks.
- Measured: 2 parallel requests ~double node output (26.5 / 20.4 tok/s); vLLM 359 tok/s at 4; monitoring
  ~1.4 GB RAM; node-1 GPU peaked at 82 °C.

## 2026-11-03 — Week 2: model serving
- Changed: SmolLM3-3B on every backend (ADR 0005); `tollgate-local` StorageClass with one PV per node
  (ADR 0006); llama-server StatefulSet, one pod per GPU node, plus Services; vLLM compose on the workstation.
- Broke: vLLM 0.30+ rejects Red Hat's build (`actorder: group`), so we use a community one. Docker Desktop
  4.30 broke CUDA in containers until updated. Docker's firewall rule exposed its ports on Public networks.
- Measured: generation 12–14 tok/s per node, ~108 tok/s on vLLM; node pod restart 15 s.

## 2026-11-01 — Cluster prep
- Changed: SSH key auth to both nodes; kubectl access from the workstation (`tollgate` context);
  NVIDIA driver packages held on both nodes; ADR 0004 (llama-server on the nodes, vLLM on the workstation).
- Broke: node-1 had an NVIDIA driver/library version mismatch until a reboot. The node GPUs are compute
  capability 5.0, too old for vLLM, which led to ADR 0004. Docker Desktop's kubectl (v1.29) is too old for
  the v1.35 cluster; still open.
- Measured: both nodes Ready, one GPU each.

## 2026-11-01 — Week 1: repo scaffold
- Changed: uv workspace with `gateway` and `billing` packages (src layout, one smoke test each);
  shared Ruff + pytest config; GitHub Actions CI (lint, format check, tests); Apache-2.0 LICENSE;
  `deploy/` and `bench/` placeholders; public repo on GitHub.
- Broke: first CI run failed because `astral-sh/setup-uv` has no floating `v10` tag, so it's pinned to
  `v10.3.3`. The gh token also lacked the `workflow` scope needed to push workflow files.
- Measured: 2/2 tests pass; CI green in 10s.

## 2026-10-31 — Kickoff
- Changed: project brief, plan, architecture and first three ADRs written.
- Broke: nothing yet.
- Measured: nothing yet. Next: week 1 scaffold.
