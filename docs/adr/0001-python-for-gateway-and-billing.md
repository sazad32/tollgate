# 0001 — Python for the gateway and billing

- Status: accepted
- Date: 2026-10-31

## Context
The owner's background is C#/.NET. The project targets AI-lab engineering roles, where Python dominates
serving, tooling and interviews.

## Options considered
1. Python (FastAPI, asyncio) — matches vLLM and AI tooling; shows range beyond .NET; async has sharp edges under load
2. TypeScript (Node) — strong for product/web roles, less aligned with infra and billing work
3. C#/.NET — fastest to build, but adds nothing new to the résumé

## Decision
Python 3.12 for gateway and billing. TypeScript only for the Phase 5 dashboard.

## Consequences
Async pitfalls (blocking calls, backpressure, pool limits) are expected; Phase 4 load tests will surface
them and the fixes get written up rather than hidden.
