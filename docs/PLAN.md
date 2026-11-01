# Tollgate — plan

22 weeks at roughly 8–10 hours a week. Six phases. Do not start a phase until the previous gate is met.

## Stages
| Stage | Weeks | Where it runs | Data |
| --- | --- | --- | --- |
| 1. Public core (this repo) | 1–17 | Home k3s cluster, not exposed to the internet | Synthetic only |
| 2. Live demo | 18–22 | Home cluster behind Cloudflare Tunnel, demo API keys, capped hosted spend | Synthetic only |
| 3. Private client build | Later | Client infrastructure or a Canadian cloud region, separate private repo | Client data stays with the client |

## Phase 1 — Serve a model (weeks 1–4)
- [x] Week 1: Repo scaffold (uv workspace, Ruff, pytest, GitHub Actions); scenario in README; first ADRs
- [ ] Week 2: StorageClass for model weights; deploy `llama-server` with one replica per GPU node; vLLM in
  Docker on the workstation (ADR 0004)
- [ ] Week 3: Prometheus + Grafana; scrape model-server metrics (`llama-server`, vLLM) and GPU use
- [ ] Week 4: Benchmark TTFT and tokens/sec at 1, 8, 32, 64 concurrent users on each backend; publish the table

**Gate:** benchmark table published in `bench/RESULTS.md`.

## Phase 2 — Gateway (weeks 5–9)
- [ ] Week 5: FastAPI skeleton, OpenAI-compatible `/v1/chat/completions`, streaming end to end to vLLM
- [ ] Week 6: Hashed API keys per tenant; Keycloak in cluster; JWT validation
- [ ] Week 7: Token-based rate limits in Redis: reserve an estimate up front, settle when the stream ends
- [ ] Week 8: Router: local by default, hosted for overflow or flagged requests; per-tenant data policy
- [ ] Week 9: Per-tenant queues, fair scheduling, 429 with retry hints; two-tenant demo

**Gate:** two tenants share one API with separate limits.

## Phase 3 — Metering and billing (weeks 10–13)
- [ ] Week 10: Usage event schema with idempotency keys, published to Redis Streams
- [ ] Week 11: Billing consumer writes a usage ledger in Postgres; partial and cancelled streams handled
- [ ] Week 12: Pricing tiers per model; monthly invoices per tenant
- [ ] Week 13: Nightly reconciliation of ledger vs model-reported tokens; draft blog post 1

**Gate:** invoices match metered tokens exactly.

## Phase 4 — Reliability and security (weeks 14–17)
- [ ] Week 14: Load tests; find where async Python breaks
- [ ] Week 15: Fix bottlenecks; re-run benchmarks; record before/after
- [ ] Week 16: Chaos drills: kill a vLLM pod mid-stream, take a node offline, stop the billing consumer
- [ ] Week 17: Security pass: Cloudflare Tunnel, TLS, secrets, RBAC, NetworkPolicies

**Gate:** latency targets hold under load.

## Phase 5 — Dashboard (weeks 18–19)
- [ ] Week 18: React + TypeScript dashboard: tenants and usage charts
- [ ] Week 19: Invoice view; tenant-scoped login via Keycloak

**Gate:** a tenant can see their own bill.

## Phase 6 — Polish and publish (weeks 20–22)
- [ ] Week 20: 3 real tenants onboarded; README with diagram and one-command deploy
- [ ] Week 21: Publish both blog posts; finalize benchmark page
- [ ] Week 22: Résumé and LinkedIn updated; 2-minute pitch rehearsed

## Hard problems to document as we go
1. Billing a stream that dies halfway
2. Rate limiting by tokens when output length is unknown
3. Exactly-once usage events across retries and restarts
4. Fairness when the GPU is saturated
5. Routing and fallback: cost and data policy
6. Async Python under load
