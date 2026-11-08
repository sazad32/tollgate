# Tollgate — project brief for Claude Code

## What this is
Tollgate is a self-hosted AI platform: an open model served on local GPUs, behind a gateway that handles
auth, token-based rate limits, routing, streaming, usage metering and billing.

Scenario we design for: a mid-size telecom wants its support, billing and engineering teams using LLMs,
but customer data can't leave its network. Every team (tenant) calls one API, sensitive traffic stays on
local GPUs, hard or overflow requests go to a hosted model, and each tenant is billed for exactly what it used.

This is a PUBLIC portfolio repo (Stage 1 of 3). Owner: Saimoon. Read `docs/PLAN.md` for phases and gates,
`docs/architecture.md` for the design, and `docs/adr/` for decisions already made.

## Current phase
**Phase 3 — Metering and billing (weeks 10–13).** Phase 2 gate met 2026-11-07 (bench/RESULTS.md, two-tenant demo). Update this line at every gate.
Do not build anything from a later phase unless asked.

## Repo layout (target)
```
gateway/      Python package `tollgate_gateway` — FastAPI gateway (Phase 2)
billing/      Python package `tollgate_billing` — usage consumer, pricing, invoices (Phase 3)
deploy/       k3s manifests (llama-server, later Redis, Postgres, Keycloak, monitoring), Helm chart (Phase 6),
              workstation/vllm/ Docker Compose for vLLM
bench/        Python package `tollgate_bench` — model-server benchmark (asyncio + httpx, ADR 0008) + RESULTS.md
dashboard/    React + TypeScript (Phase 5 only)
docs/         PLAN.md, architecture.md, adr/, dev-log.md
```
Python is a uv workspace at the repo root. Each package has `src/` layout and its own `tests/`.

## Stack (decided — see docs/adr before proposing changes)
- Python 3.12, uv, FastAPI, httpx (async), Pydantic v2, pytest + pytest-asyncio, Ruff
- Model servers, all OpenAI-compatible (ADR 0004): llama.cpp `llama-server` on the k3s nodes, one replica
  per GPU node; vLLM in Docker on the workstation; optional rented cloud GPU for benchmarks
- Redis (rate limits, per-tenant queues) and Redis Streams (usage events)
- PostgreSQL (usage ledger, pricing, invoices) — SQLAlchemy 2.0 async + Alembic
- Keycloak (OIDC) + hashed API keys
- Hosted fallback: Anthropic API via the official `anthropic` SDK, model name from config
- Prometheus, Grafana, OpenTelemetry
- k3s (2 nodes, 1 GPU each), Helm, GitHub Actions, GitHub Container Registry

## Environment facts
- node-1 and node-2 each advertise `nvidia.com/gpu: 1`. node-1 is the k3s server (control plane); node-2 is an agent.
- k3s v1.35. node-1 runs Ubuntu 24.04 (kernel 6.8); node-2 runs Ubuntu 26.04 (kernel 7.0). NVIDIA driver
  packages are on `apt-mark hold` on both nodes: update the driver and reboot together. node-2's core
  tools are uutils (Rust coreutils): e.g. `install -g <number>` fails without a named group; use `chown`.
- Monitoring (ADR 0007): kube-prometheus-stack in the `monitoring` namespace, installed by k3s's Helm
  controller (`HelmChart`), so no Helm CLI is needed. Grafana's login is the hand-made `grafana-admin` Secret.
- Already in the cluster: Traefik ingress, `local-path` provisioner (default StorageClass), metrics-server,
  NVIDIA device plugin. Tollgate uses its own `tollgate` namespace; leave `default` alone.
- kubectl on the workstation uses the `tollgate` context (kubeconfig in the user's home, never in the repo).
- Persistent storage: `tollgate-local` StorageClass with static local PVs (ADR 0006). node-2's `/mnt/storage`
  is a 458 GB data disk; on node-1 it's a folder on the root disk. Model weights live at
  `/mnt/storage/tollgate/models` on both nodes. Durable data (Postgres) goes on node-2.
- GPU pods need `runtimeClassName: nvidia` and `nodeSelector: gpu: "true"`.
- Node GPUs: node-1 GeForce GTX 860M, node-2 GTX 960M. Both 4 GB VRAM, compute capability 5.0, driver 580. Too old for vLLM
  (needs 7.0+). Use CUDA 12 builds; CUDA 13 dropped this generation.
- Workstation (Saimoon's Windows PC, outside the cluster): RTX 3060 Laptop GPU, 6 GB VRAM, compute
  capability 8.6, Docker Desktop on WSL2. Runs vLLM. Not always on. Windows uses 1–2 GB of this GPU for
  the display, so vLLM gets about 4 GB. The PC has 15 GB of RAM; starting vLLM needs about 2.5 GB free on
  top of Docker's VM, so close or cap the browser first. Run vLLM steps in the foreground, with a RAM check.
- Gateway (ADR 0009): image `ghcr.io/sazad32/tollgate-gateway:sha-<commit>`, built by
  `.github/workflows/gateway-image.yml` on `main` and `feat/**`; deploy pins it by digest (in both
  `deployment.yaml` and `migrate-job.yaml`).
- Auth (ADR 0010): every `/v1` call needs a tenant API key (`tg_…`) or a Keycloak token. Tenants: `support`
  and `billing` (local_only), `engineering` (hosted_allowed). Postgres 18 (`postgres-0`, node-2) holds
  tenants and keys (ADR 0011); Keycloak 26 issues tokens (realm `tollgate`). Secrets `postgres-credentials`,
  `keycloak-admin`, `keycloak-clients`, `gateway-auth`, `anthropic-api-key` are hand-made (see
  deploy/README.md); creating Secrets is the user's step, not Claude's.
- Rate limits (ADR 0012): token bucket per tenant in Redis (`deploy/redis`, Secret `redis-auth`), reserve an
  estimate, settle with real usage; tiers in `TOLLGATE_RATE_LIMITS`. Redis down = no limits (fail open).
- Routing (ADR 0013): backends in `TOLLGATE_BACKENDS` (vLLM priority 0, each llama-server pod via the
  headless Service), load polled from their /metrics every second. Hosted model `claude-opus-5-5` via the
  `anthropic` SDK, only for `hosted_allowed` tenants; off without the `anthropic-api-key` Secret.
  Responses carry `x-tollgate-backend`.
- Fair queuing (ADR 0014): local requests wait in Redis (`tollgate:queue:*`) for a slot on a specific
  backend; the tenant with fewest running goes next; 429 + Retry-After past 16 waiting or 120 s. Leases
  (30 s, renewed every 10 s) free a dead replica's slots. `TOLLGATE_FAIR_QUEUE=false` turns it off.
- Usage events (ADR 0015, format in `docs/usage-events.md`): one per request a model ran, in the Redis
  stream `tollgate:usage`, key `<request id>:usage`, at least once (buffered in memory while Redis is
  down). Redis is a StatefulSet `redis-0` on node-2 with an append-only file on a local PV.
- Billing (ADR 0016): Deployment `tollgate-billing` (image `ghcr.io/sazad32/tollgate-billing`, built by
  `billing-image.yml`, pinned by digest in `deployment.yaml` and `migrate-job.yaml`). It reads the stream
  as consumer group `billing`, and acknowledges after commit into `usage_ledger` (PK = idempotency key) in
  its own database `billing` (login `billing`, Secret `billing-db`, made by `db-init-job.yaml`). Bad entries
  go to `usage_rejects`. Billing rule: completed = all, client_disconnected = estimate, failed = 0. Trims
  the stream only past 24 h and acknowledged by every group. Ledger SQL tests need a real Postgres
  (`TOLLGATE_TEST_DATABASE_URL`; CI has one, locally see billing/README.md), else they skip.

## Rules
1. **Never commit secrets.** Config via env vars and Kubernetes Secrets. Ship `.env.example` only.
2. **Synthetic data only.** No real customer or personal data in this repo, ever.
3. **Tests first for core logic.** Rate limiting, token reservation, usage events, metering, pricing and
   invoicing: write failing tests, show them, then implement.
4. **Explain the core.** For rate limiting, metering and billing, explain the design and tradeoffs before
   writing code. Saimoon must be able to defend every line in an interview — prefer clear over clever.
5. **Billing never sits in the request path.** The gateway emits usage events; billing consumes them
   asynchronously. A billing outage must not take the API down.
6. **Idempotency everywhere money is involved.** Every usage event carries an idempotency key.
7. **Small, reviewable changes.** One concern per commit. Conventional commit messages.
8. **Ask before adding a dependency** not listed in the stack above.
9. **Record decisions.** Any non-trivial design choice gets an ADR in `docs/adr/` (copy the template).
10. **Log the week.** At the end of a work session, append a short entry to `docs/dev-log.md`:
    what changed, what broke, what was measured.
11. **Explain in .NET terms, briefly.** Saimoon is a C#/.NET developer. When explaining Python, APIs or
    libraries, follow the `dotnet-bridge` skill: lead with the C#/.NET equivalent, flag where the
    analogy breaks, keep it short and simple.

## Commands
```
uv sync                         # install workspace
uv run pytest                   # all tests
uv run ruff check --fix . && uv run ruff format .
kubectl get pods -A             # cluster state
kubectl apply -k deploy         # create or update everything Tollgate runs in k3s
kubectl -n tollgate get pods -o wide                             # llama-server: one pod per GPU node
kubectl -n tollgate port-forward svc/llama-server 8080:8080      # call llama-server from the workstation
docker compose -f deploy/workstation/vllm/compose.yaml up -d     # vLLM on the workstation (needs .env)
docker compose -f deploy/workstation/vllm/compose.yaml down      # stop vLLM, free the GPU
kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80        # Grafana (Tollgate folder)
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090   # Prometheus, /targets
kubectl -n tollgate port-forward svc/tollgate-gateway 8000:8000  # the gateway, http://localhost:8000/v1
kubectl -n tollgate exec deploy/tollgate-gateway -- python -m tollgate_gateway.admin tenant list   # also: key create|list|revoke
kubectl apply -f deploy/gateway/migrate-job.yaml   # schema migrations, once per release (then delete the Job)
uv run uvicorn --factory tollgate_gateway.main:create_app --reload  # gateway locally; needs TOLLGATE_API_KEY_PEPPER and TOLLGATE_DATABASE_URL
uv run python -m tollgate_bench job node-2 | kubectl apply -f -   # benchmark a backend (see bench/README.md)
uv run python -m tollgate_bench report bench/raw/*.jsonl         # Markdown results table from Job logs
uv run python -m tollgate_bench fairness --url ... --trickle-url ...  # two-tenant demo (bench/README.md)
kubectl -n tollgate exec redis-0 -- sh -c 'redis-cli --no-auth-warning -a "$REDIS_PASSWORD" XLEN tollgate:usage'  # usage events
kubectl -n tollgate exec deploy/tollgate-billing -- python -m tollgate_billing status   # billing: group, ledger, per tenant
kubectl apply -f deploy/billing/migrate-job.yaml   # billing's migrations, once per release (then delete the Job)
docker run -d --rm --name tollgate-test-pg -e POSTGRES_PASSWORD=test -p 127.0.0.1:54329:5432 postgres:18.6-bookworm  # then TOLLGATE_TEST_DATABASE_URL (billing/README.md)
```
(Add new commands here as they are created.)

## Definition of done for any task
Tests pass, Ruff clean, docs updated if behaviour changed, dev-log entry added.
