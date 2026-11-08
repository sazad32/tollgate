# Usage events (schema version 1)

The gateway writes one event per request a model ran to the Redis stream `tollgate:usage`
(ADR 0015). Billing reads them from there (week 11). This page is the contract between the two.

## When there is an event
- **Yes:** a model accepted the request (HTTP 200 from a local backend, or a reply from the hosted
  model), however it then ended: finished, the client left, or the stream broke.
- **No:** nothing ran. That covers 401 and 403, 429 (token limit or queue), a backend that couldn't be
  reached, a backend's 4xx or 5xx answer, and a hosted-model error before its first token.

## Fields
Every value is a string (a Redis stream entry holds strings).

| Field | Example | Meaning |
| --- | --- | --- |
| `v` | `1` | Schema version. A new field keeps the version; a changed meaning bumps it. |
| `idempotency_key` | `9f2c…e1:usage` | `<request_id>:usage`. Unique per request: count each key once. |
| `request_id` | `9f2c…e1` | 32 hex characters, made by the gateway (never by the client); also the `X-Request-ID` response header. |
| `tenant_id` | `support` | The tenant billed. |
| `credential` | `key:4d1a…` or `token:service-account-support` | The API key's id, or the Keycloak token's subject. |
| `model` | `smollm3-3b`, `claude-opus-5-5` | The model that answered. After an overflow or a refusal fallback, not the one asked for. |
| `backend` | `vllm`, `llama-server-0`, `hosted` | Where it ran. |
| `input_tokens` | `74` | Prompt tokens. |
| `output_tokens` | `60` | Generated tokens. For Claude, thinking tokens count here, as Anthropic bills them. |
| `token_source` | `reported` / `estimated` | `reported`: the model's own count. `estimated`: the client left (or the stream broke) before the model reported; input is the gateway's prompt estimate (characters ÷ 3 + 100), output one token per chunk sent. |
| `status` | `completed` / `client_disconnected` / `failed` | How the request ended. `failed`: the stream broke mid-way. |
| `stream` | `true` / `false` | Whether the client asked for a stream. |
| `started_at` | `2026-11-08T17:22:04.519Z` | When the gateway received the request (UTC, ISO 8601, milliseconds). |
| `finished_at` | `2026-11-08T17:22:06.002Z` | When it ended. |

Hosted requests that fell back after a refusal count every attempt, as Anthropic bills them.

## Delivery
- **At least once.** The gateway retries writes, so the same event can appear twice in the stream.
  Consumers must deduplicate by `idempotency_key`.
- **Order** is roughly by end time, per gateway replica. Don't rely on it.
- **Loss** is possible in one case: a gateway pod that crashes while Redis is down loses the events
  it was holding (up to 10,000). An event dropped because that buffer was full is logged in full at
  ERROR level (`usage event dropped`), so it can be replayed by hand.

## Reading it
```powershell
kubectl -n tollgate exec redis-0 -- sh -c 'redis-cli --no-auth-warning -a "$REDIS_PASSWORD" XLEN tollgate:usage'
kubectl -n tollgate exec redis-0 -- sh -c 'redis-cli --no-auth-warning -a "$REDIS_PASSWORD" XRANGE tollgate:usage - + COUNT 5'
```
In Python: `UsageEvent.from_fields(fields)` in `tollgate_gateway/usage.py` reads an entry back.
