# 0010 — Tenant authentication: hashed API keys and Keycloak tokens

- Status: accepted
- Date: 2026-11-07

## Context
From week 6 on, every `/v1` request has to say which tenant it's for: rate limits (week 7), routing by
data policy (week 8) and billing (week 10) all hang off the tenant. ADR 0003 settled the shape: Keycloak
issues tokens and the gateway only validates them, alongside per-tenant API keys stored hashed. This ADR
settles the details: key format and hashing, what a token must contain, caching, and how failures look
to a caller.

## Options considered
1. **Key hashing:** bcrypt or argon2 (deliberately slow, built for passwords), or HMAC-SHA256 with a
   server-side secret (fast, deterministic).
2. **Key lookup:** a key ID in the key plus a hash to compare, or look the key up by its hash directly.
3. **Tenant in tokens:** a hard-coded claim per Keycloak client, group membership, or a mapping table in
   the gateway.
4. **Caching:** check the database on every request, or cache checked credentials for a short time.

## Decision
- **Keys are `tg_` plus 256 random bits**, shown once at creation. The database stores an HMAC-SHA256 of
  the key, keyed with a pepper held only in a Kubernetes Secret, and the key's first 11 characters for
  listings. Keys are random, not human-chosen, so a slow hash adds latency without adding safety; the
  pepper means a stolen database copy can't be used to test guesses. The hash is deterministic, so the
  lookup is a plain unique-index search.
- **Tokens:** RS256 only, whatever the token's header says; signature checked against the realm's JWKS;
  expiry with 30 s leeway; issuer `http://keycloak.tollgate.svc:8080/realms/tollgate` (pinned in
  Keycloak, so it's the same however Keycloak was reached); audience `tollgate-gateway`; and a `tenant`
  claim, hard-coded per client by a Keycloak mapper. The tenant must exist and be enabled in the
  gateway's own database.
- **JWKS caching:** keys cached by `kid`. An unknown `kid` triggers one re-fetch (key rotation), then at
  most one every 30 s, so forged tokens can't make the gateway hammer Keycloak.
- **Credential cache:** accepted keys and looked-up tenants are cached for 60 s per gateway pod.
- **Responses** follow OpenAI's error format: 401 for missing or bad credentials (with
  `WWW-Authenticate: Bearer`), 403 for disabled or unregistered tenants, and **503 when the database or
  Keycloak is down**. An outage on our side must never tell a caller its credential is wrong.
- **"Last used"** is recorded only for accepted keys, on cache misses.
- **Administration** is a CLI in the gateway image (`python -m tollgate_gateway.admin`), run with
  `kubectl exec`; there's no admin HTTP API yet.

## Consequences
- **Revocation and tenant disabling take up to 60 s** (one cache lifetime per pod). Measured: a revoked
  key kept working until its cache entry expired, then got 401; a disabled tenant got 403 after 62 s.
- **Cost:** a cached credential adds nothing measurable. A cache miss adds roughly 35–170 ms (lookup and
  recording the use, over a pooled connection to Postgres on the other node). Recording the use in the
  background would take the second round trip off the request path; not done yet.
- Unknown keys aren't cached, so every bad key costs a database lookup; an ingress rate limit in week 17
  limits that.
- Tokens are verified on every request (an RSA signature check, well under a millisecond); only the
  signing keys and the tenant record are cached.
- The tenant mapping lives in two places: the Keycloak client's mapper and the gateway's `tenants` table.
  Adding a tenant means both, until there's onboarding tooling.
- Postgres became a dependency of every API-key request (ADR 0011). Its outage turns into 503s within
  a minute, when cached entries expire.
