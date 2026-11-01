# Tollgate — architecture

## Request path (synchronous)
```
Tenant (API key or OIDC token)
  -> Gateway (FastAPI)
       1. Authenticate: hashed API key lookup, or JWT validated against Keycloak's signing keys
       2. Admit: per-tenant token budget check in Redis (reserve an estimate)
       3. Route: local vLLM (default) or hosted model (overflow / flagged / policy allows)
       4. Stream: proxy SSE chunks back to the client, counting tokens as they pass
       5. Settle: release unused reservation; emit one usage event
  -> vLLM on node-1 GPU | vLLM on node-2 GPU | Hosted model (Anthropic API)
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
| Gateway | k3s Deployment, 2 replicas | Stateless |
| vLLM | 1 pod per GPU node (nodeSelector / GPU request) | Model weights on PV |
| Redis | k3s StatefulSet | Rate-limit counters, queues, usage stream |
| Postgres | k3s StatefulSet on node-2 storage | Ledger, pricing, invoices, tenants |
| Keycloak | k3s Deployment | Its own Postgres schema |
| Prometheus + Grafana | k3s, monitoring namespace | Metrics |
| Dashboard | Phase 5 | Reads billing API |

## Key invariants
- A billing outage must not fail API requests. Usage events buffer in Redis Streams.
- A usage event is counted exactly once (idempotency key = request id + event type).
- A tenant's data policy decides whether a request may leave the network. Default: local only.
- Tokens are billed on what the model generated, with partial streams recorded explicitly (see ADR when written).

## Data model (initial sketch — refine in Phase 3 ADRs)
- `tenants(id, name, data_policy, tier)`
- `api_keys(id, tenant_id, key_hash, created_at, revoked_at)`
- `usage_events(idempotency_key PK, tenant_id, request_id, model, input_tokens, output_tokens, status, occurred_at)`
- `prices(model, tier, input_per_1k, output_per_1k, effective_from)`
- `invoices(id, tenant_id, period, total, status)` + `invoice_lines`

## Out of scope for the public repo
Client-specific billing adapters, client SSO wiring, client deployment manifests (Stage 3, private repo).
