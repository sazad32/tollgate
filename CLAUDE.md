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
**Phase 1 — Serve a model (weeks 1–4).** Update this line at every gate.
Do not build anything from a later phase unless asked.

## Repo layout (target)
```
gateway/      Python package `tollgate_gateway` — FastAPI gateway (Phase 2)
billing/      Python package `tollgate_billing` — usage consumer, pricing, invoices (Phase 3)
deploy/       Helm chart and k3s manifests (vLLM, Redis, Postgres, Keycloak, monitoring)
bench/        Load and benchmark scripts (k6 or Locust) + results
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
- Persistent storage: node-2 at `/mnt/storage` (local PV today; a StorageClass is a Phase 1 task)
- Node GPUs: node-1 GeForce GTX 860M, node-2 GTX 960M. Both 4 GB VRAM, compute capability 5.0, driver 580. Too old for vLLM
  (needs 7.0+). Use CUDA 12 builds; CUDA 13 dropped this generation.
- Workstation (Saimoon's Windows PC, outside the cluster): RTX 3060 Laptop GPU, 6 GB VRAM, compute
  capability 8.6, Docker Desktop on WSL2. Runs vLLM. Not always on.

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
```
(Add new commands here as they are created.)

## Definition of done for any task
Tests pass, Ruff clean, docs updated if behaviour changed, dev-log entry added.
