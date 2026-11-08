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
Gateway -> Redis Streams (usage events, idempotency key per event)
        -> Billing consumer -> Postgres usage ledger
                            -> Pricing -> monthly invoices per tenant
        -> Nightly reconciliation: ledger vs model-reported token counts
```

## Components
| Component | Runs where | State |
| --- | --- | --- |
| Gateway | k3s Deployment, 2 replicas, image from GHCR (ADR 0009) | Stateless; polls backend load every second (ADR 0013) |
| llama-server | k3s StatefulSet, 1 pod per GPU node (ADR 0004, 0006) | Model weights on a local PV per node |
| vLLM | Docker on the workstation, outside k3s (ADR 0004) | Model weights in a Docker volume |
| Hosted model | Anthropic API, outside the network; Claude via the `anthropic` SDK (ADR 0013) | None of ours |
| Redis | k3s Deployment, no persistence yet (ADR 0012) | Token buckets, the fair queue with its leases (ADR 0014); the usage stream next (week 10, needs persistence) |
| Postgres | k3s StatefulSet on node-2 storage (ADR 0011) | Tenants and API keys now; ledger, pricing, invoices in Phase 3 |
| Keycloak | k3s Deployment, realm `tollgate` (ADR 0010) | Its own database in the same Postgres |
| Prometheus + Grafana | k3s `monitoring` namespace, kube-prometheus-stack (ADR 0007) | Metrics on a local PV on node-2 |
| DCGM exporter | k3s DaemonSet on GPU nodes (ADR 0007) | Stateless |
| Dashboard | Phase 5 | Reads billing API |

## Key invariants
- A billing outage must not fail API requests. Usage events buffer in Redis Streams.
- A usage event is counted exactly once (idempotency key = request id + event type).
- A tenant's data policy decides whether a request may leave the network. Default: local only, and any
  value other than `hosted_allowed` counts as local only. The router checks it, and the hosted adapter
  checks it again before anything leaves (ADR 0013).
- Tokens are billed on what the model generated, with partial streams recorded explicitly (see ADR when written).

## Data model (initial sketch — refine in Phase 3 ADRs)
- `tenants(id, name, data_policy, tier, disabled, created_at)` (built in week 6)
- `api_keys(id, tenant_id, name, prefix, key_hash, created_at, last_used_at, revoked_at)` (built in week 6)
- `usage_events(idempotency_key PK, tenant_id, request_id, model, input_tokens, output_tokens, status, occurred_at)`
- `prices(model, tier, input_per_1k, output_per_1k, effective_from)`
- `invoices(id, tenant_id, period, total, status)` + `invoice_lines`

## Out of scope for the public repo
Client-specific billing adapters, client SSO wiring, client deployment manifests (Stage 3, private repo).
