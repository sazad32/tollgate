# 0003 — Keycloak for identity, not a custom IdP

- Status: accepted
- Date: 2026-10-31

## Context
The owner has already built an OIDC authorization server professionally. Building another adds little
portfolio value and costs weeks.

## Options considered
1. Custom OIDC server — no new learning, high cost
2. Keycloak — standard, self-hosted, fits the "data stays in network" scenario
3. Hosted IdP (Auth0, etc.) — contradicts the self-hosted scenario

## Decision
Keycloak issues tokens; the gateway only validates JWTs (via JWKS) and also accepts hashed per-tenant API keys.

## Consequences
Time goes to metering and billing instead. Keycloak needs its own database schema and a realm export in `deploy/`.
