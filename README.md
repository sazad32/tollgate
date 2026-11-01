# Tollgate

A self-hosted AI platform: open models on local GPUs behind a gateway that handles auth, token-based
rate limits, routing, streaming, usage metering and per-tenant billing.

> Status: early development — Phase 1 (serving a model). See [docs/PLAN.md](docs/PLAN.md).

## Why
A mid-size telecom wants its support, billing and engineering teams using LLMs, but customer data can't
leave its network. Tollgate gives every team one API, keeps sensitive traffic on local GPUs, sends hard or
overflow requests to a hosted model, and bills each team for exactly what it used.

## Design
See [docs/architecture.md](docs/architecture.md) and the decision records in [docs/adr/](docs/adr/).

## Benchmarks
Coming in Phase 1: time-to-first-token, tokens/sec and p99 latency at 1, 8, 32 and 64 concurrent users.

## Running it
Coming soon: one-command deploy to k3s via Helm.

## Development
Needs [uv](https://docs.astral.sh/uv/), which installs Python 3.12 for you.
```
uv sync                                              # install the workspace
uv run pytest                                        # run all tests
uv run ruff check . && uv run ruff format --check .  # lint and format check
```

## License
Apache-2.0
