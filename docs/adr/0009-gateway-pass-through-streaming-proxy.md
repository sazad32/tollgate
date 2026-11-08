# 0009 — Gateway: a pass-through streaming proxy, built in CI, run in k3s

- Status: accepted
- Date: 2026-11-07

## Context
Phase 2 starts with a gateway that speaks the OpenAI API and streams answers from the model servers
(`llama-server` in k3s, vLLM on the workstation). Auth (week 6), rate limits (week 7), routing (week 8)
and usage events (week 10) build on it, so its shape matters more than its features. The week 4
benchmark gave it two requirements: survive llama-server's 5 s keep-alive race, and don't waste GPU
time on clients that have gone away.

## Options considered
1. **Request handling:** model OpenAI's whole request schema in Pydantic, or check only the fields the
   gateway needs and forward the body untouched. A full schema documents itself, but it breaks on every
   new field and on server-specific ones (`ignore_eos`).
2. **Streaming:** buffer and forward, or relay chunk by chunk and propagate a client disconnect to
   the model server.
3. **Retries:** none, retry on any failure, or retry only failures that prove the model never saw
   the request.
4. **Running it:** build images by hand and import them into k3s (needs sudo on the nodes), or
   build in GitHub Actions and pull from GitHub Container Registry.

## Decision
- **Pass-through proxy.** Validate `model`, `messages` and `stream`; forward the original JSON. Errors
  use OpenAI's format: 400 for bad requests, 502 for model-server errors (details not leaked), 503
  when it's unreachable, 504 when it times out. Client errors from the model server (e.g. prompt too
  long) pass through unchanged.
- **Chunk-by-chunk streaming with disconnect propagation.** When a client goes away, Starlette cancels
  the relay and the gateway closes the upstream request. The close is shielded from anyio's
  cancellation, which otherwise re-fires at every `await` and would interrupt the close itself. This
  relies on Uvicorn reporting ASGI spec 2.3; from 2.4 on, Starlette only notices a disconnect when it
  next sends.
- **One retry, only when it's safe.** Retry once on a refused connection or a connection closed before
  any response arrived. Never on timeouts or server errors: the model may already be generating.
  Upstream keep-alive expires at 2 s, under llama-server's 5 s.
- **A server-made request ID** (`X-Request-ID`) on every request, returned and sent upstream. Clients
  can't choose it, because week 10's billing idempotency keys build on it.
- **One configured upstream** (`TOLLGATE_UPSTREAM_URL`), the llama-server Service by default. (Replaced
  by the router in week 8: ADR 0013.)
- **CI-built image, pinned by digest.** A two-stage uv build, run as 10001:10001 with a read-only root
  filesystem; GitHub Actions pushes `ghcr.io/sazad32/tollgate-gateway:sha-<commit>` on `main` and
  `feat/**`. The package is public, like the repo, so k3s pulls it without credentials.
- **Readiness isn't tied to the model server.** Otherwise one model-server outage would pull every
  gateway replica out of its Service, and clients would get connection errors instead of a clear 503.

## Consequences
- Measured overhead is 3–7 ms on time to first token and none on time per token: against vLLM, 80 ms vs
  77 ms at 1 user and 460 ms vs 454 ms at 8. One unexplained tail: p95 at 8 users was 599 ms vs 477 ms,
  in a single run. Phase 4 measures it under load.
- A real disconnect stopped llama-server after 49 of 400 requested tokens.
- Unknown request fields reach the model server unchecked. Week 7's token reservation will need to
  read `max_tokens`, at which point it becomes a validated field.
- Deploying a code change means a new image digest in `deploy/gateway/deployment.yaml`, a manual step
  until there's a release flow.
- Images are built for feature branches too, so the registry collects `sha-` tags; prune them later.
- The 120 s termination grace lets most streams finish during a rollout, but a llama-server request
  queued for minutes can still be cut off.
