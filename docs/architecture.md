# Tollgate — architecture

## Request path (synchronous)
```
Tenant (API key or OIDC token)
  -> Gateway (FastAPI)
       1. Authenticate: hashed API key lookup, or JWT validated against Keycloak's signing keys
       2. Admit: per-tenant token budget check in Redis (reserve an estimate)
       3. Route: a local model server by load, vLLM first while it has room (ADR 0013); the hosted
          model only for hosted_allowed tenants, when asked for by name or when local is full
          Local requests wait for a fair turn at a slot on a specific backend (ADR 0014): the
          tenant with fewest requests running goes next; 429 + Retry-After past 16 waiting or 120 s
       4. Stream: proxy SSE chunks back to the client, counting tokens as they pass
       5. Settle: release unused reservation; emit one usage event
  -> llama-server on node-1 | llama-server on node-2 | vLLM on workstation | Hosted model (Anthropic API)
```

## Usage path (asynchronous — never blocks requests)
```
Gateway -> in-memory buffer -> Redis Streams `tollgate:usage` (one event per request a model ran,
           idempotency key = request id + ":usage", at least once; ADR 0015, docs/usage-events.md)
        -> Billing consumer (consumer group `billing`; acknowledge after commit; ADR 0016)
           -> Postgres usage ledger (key = idempotency key, so a copy is skipped)
              -> Monthly invoices per tenant (CronJob, 06:00 UTC on the 1st; ADR 0017): the rows
                 recorded in the month, priced from deploy/billing/prices.toml
        -> Nightly reconciliation (CronJob, 02:00 UTC; ADR 0018): the ledger's output tokens per
           backend vs the model servers' own counters in Prometheus; fails on a gap
```

## Components
| Component | Runs where | State |
| --- | --- | --- |
| Gateway | k3s Deployment, 2 replicas, image from GHCR (ADR 0009) | Stateless; polls backend load every second (ADR 0013); its own metrics at /metrics (ADR 0019) |
| llama-server | k3s StatefulSet, 1 pod per GPU node (ADR 0004, 0006) | Model weights on a local PV per node |
| vLLM | Docker on the workstation, outside k3s (ADR 0004) | Model weights in a Docker volume |
| Hosted model | Anthropic API, outside the network; Claude via the `anthropic` SDK (ADR 0013) | None of ours |
| Redis | k3s StatefulSet on node-2, append-only file on a local PV (ADR 0015) | Token buckets, the fair queue with its leases (ADR 0014), the usage stream |
| Billing | k3s Deployment, 1 replica, image from GHCR (ADR 0016); a CronJob for the monthly invoices (ADR 0017) | Stateless; its position in the stream is kept by Redis (the consumer group); prices in a ConfigMap from Git |
| Postgres | k3s StatefulSet on node-2 storage (ADR 0011) | Tenants and API keys; billing's own database `billing` with the usage ledger and invoices (ADR 0016, 0017) |
| Keycloak | k3s Deployment, realm `tollgate` (ADR 0010) | Its own database in the same Postgres |
| Prometheus + Grafana | k3s `monitoring` namespace, kube-prometheus-stack (ADR 0007) | Metrics on a local PV on node-2 |
| DCGM exporter | k3s DaemonSet on GPU nodes (ADR 0007) | Stateless |
| Dashboard | Phase 5 | Reads billing API |

## Key invariants
- A billing outage must not fail API requests. Usage events buffer in Redis Streams.
- A usage event is counted exactly once (idempotency key = request id + event type).
- Every billed ledger row is on exactly one invoice, and a finalized invoice never changes: a month
  holds the rows recorded in it, so a closed month can't gain one (ADR 0017).
- Every night, each local model server's own count of generated tokens is checked against the
  ledger; only estimated tokens may differ (ADR 0018).
- A tenant's data policy decides whether a request may leave the network. Default: local only, and any
  value other than `hosted_allowed` counts as local only. The router checks it, and the hosted adapter
  checks it again before anything leaves (ADR 0013).
- Tokens are billed on what the model generated, with partial streams recorded explicitly: a client
  that hangs up pays the estimate, a stream that broke on our side is billed at nothing, and the
  ledger keeps the measured tokens either way (ADR 0016).

## Data model (initial sketch — refine in Phase 3 ADRs)
- `tenants(id, name, data_policy, tier, disabled, created_at)` (built in week 6)
- `api_keys(id, tenant_id, name, prefix, key_hash, created_at, last_used_at, revoked_at)` (built in week 6)
- `usage_ledger(idempotency_key PK, request_id, tenant_id, credential, model, backend, input_tokens, output_tokens, token_source, status, stream, billable_input_tokens, billable_output_tokens, started_at, finished_at, schema_version, stream_id, recorded_at)`
  in billing's own database, append-only (built in week 11, ADR 0016)
- `usage_rejects(stream_id PK, fields, reason, rejected_at)`: stream entries billing couldn't trust (week 11)
- Prices: not a table but `deploy/billing/prices.toml`, per model, per million tokens, graduated bands, versions
  from the 1st of a month (ADR 0017)
- `invoices(tenant_id + period PK, currency, requests, input_tokens, output_tokens, total, finalized_at)` (week 12)
- `invoice_lines(tenant_id + period + line_no PK, model, meter, band_from, band_up_to, tokens, per_million, amount)`
- `invoice_items(idempotency_key PK -> usage_ledger, tenant_id, period)`: which invoice each ledger row is on
- `reconciliations(day + backend + meter PK, ledger_reported, ledger_estimated, server, allowed, status, since, until, checked_at)` (week 13)

## Out of scope for the public repo
Client-specific billing adapters, client SSO wiring, client deployment manifests (Stage 3, private repo).
