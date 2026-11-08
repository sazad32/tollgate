# 0013 — Routing: local backends by load, the hosted model only where the data policy allows

- Status: accepted
- Date: 2026-11-08

## Context
Three local backends serve the same model (ADR 0005) at very different speeds: vLLM on the workstation
reaches ~1,100 tokens/s at 32 users, while a node's llama-server stays at ~17–20 and serves 2 requests at
a time (bench/RESULTS.md). vLLM comes and goes with the PC. Until now the gateway forwarded to one Service,
which spreads connections, not load. The scenario also needs a hosted model for overflow and for requests
that ask for it, while some tenants' data must never leave the network (PLAN's hard problem #5).

## Options considered
1. **Load signal:** the Service's round robin (blind to load); counters inside each gateway replica (each
   sees only half the traffic); counters in Redis (exact, but another round trip per request, and leaked
   if a pod dies mid-request); or each backend's own `/metrics`, polled (one shared truth, up to a
   second old).
2. **Choice:** round robin weighted by capacity, or "free slot first, preferred kind first".
3. **Asking for the hosted model:** a custom header, or the standard `model` field.
4. **Calling Claude:** raw HTTP, an OpenAI-compatible shim, or Anthropic's official SDK with a translation.
5. **Enforcing the data policy:** in the router only, or in the router and again where data leaves.

## Decision
- **Backends are config** (`TOLLGATE_BACKENDS`): name, URL, kind, slots, priority. Each llama-server pod
  is addressed on its own through the headless Service. The gateway reads every backend's `/metrics`
  (running + waiting requests) once a second, one loop per backend. A failed poll, or a refused
  connection, marks a backend down until a poll succeeds again.
- **A request counts against its backend the moment it is sent**, until the next poll includes it. The
  gateway chooses and counts with no `await` in between, so concurrent requests never decide on the same
  snapshot.
- **The rule** (`router.choose_route`, a pure function):
  1. A request for the hosted model by name goes there if the tenant is `hosted_allowed`, else 403.
  2. Otherwise a local backend with a free slot: the preferred kind first (vLLM, priority 0), then the
     most free slots.
  3. No free slot: a `hosted_allowed` tenant overflows to the hosted model; a `local_only` tenant waits
     in a local queue (preferred kind first, then the shortest).
  4. No local backend up: `hosted_allowed` goes hosted; `local_only` gets 503.
- **One retry, now with failover:** a request the backend never saw (refused connection, closed idle
  connection) is tried once more on the next backend in the router's order.
- **The data policy** is a tenant field. Only the exact value `hosted_allowed` lets a request leave;
  anything else, including a typo, keeps it local (fail closed). The router enforces it, and the hosted
  adapter checks it again just before any data leaves (`DataPolicyError`). `/v1/models` lists the hosted
  model only for tenants allowed to use it.
- **The hosted model** is Claude through the official `anthropic` SDK, model `claude-opus-5-5` at effort
  `low` (both config). An adapter translates OpenAI chat requests to the Messages API and replies and
  stream events back, so the rest of the gateway, including rate-limit settlement, sees only OpenAI
  format. Anthropic's server-side refusal fallback (`fallbacks: "default"`) is on, and usage counts every
  attempt. Text only for now: tools, images, JSON mode and `n > 1` are refused, and never overflow.
- **Errors:** Anthropic's 429 or 529 become our 503 (it's our capacity, not the tenant's limit); 400
  stays 400; a bad API key and Anthropic's own failures are 502; a timeout is 504.
- **Every response names its backend** in `x-tollgate-backend` (`vllm`, `llama-server-0`, `hosted`...).

## Consequences
- **Verified in the cluster:**
  - vLLM took every request while it had room (six at once included).
  - Stopping vLLM mid-use caused no client errors: the next requests went to the nodes.
  - Five concurrent streams split 3 and 2 over the nodes. (They split 1 and 4 before requests were
    counted on sending; that bug was found here.)
  - With the nodes full, `support` (`local_only`) waited in a local queue while `engineering`
    (`hosted_allowed`) overflowed to Claude.
  - `support` asking for Claude got 403, and nothing reached Anthropic.
  - Claude replies and streams came back in OpenAI format with usage.
- **Up to a second of blindness between replicas:** each replica counts only its own sends between
  polls, so a burst split across both can overfill a node briefly. It then waits in llama-server's queue.
- **Overflow changes the model a client gets.** The response's `model` and `x-tollgate-backend` say so;
  pricing (week 12) must charge by the model that answered.
- **Hosted tokens share the tenant's token bucket** (ADR 0012), counted by Claude's tokenizer. Thinking
  counts as output, as Anthropic bills it. Dollar caps come with pricing and the live demo's spend cap.
- **Polling cost:** 6 small requests a second (3 backends × 2 replicas). While the PC is off, vLLM's
  poll times out each second without delaying the others.
- **The SDK adds a second HTTP library** (httpx2) and ~45 MiB per gateway pod (70 → 117 MiB of 256).
  Its own stream close isn't shielded from cancellation, but the transport under it shields the socket
  close, so a client that leaves still stops Claude (test_streaming_disconnect.py).
- **Local queues are first come, first served across tenants.** Fair per-tenant queues are week 9.
