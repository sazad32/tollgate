# 0011 — Postgres in the cluster, from week 6

- Status: accepted
- Date: 2026-11-07

## Context
The plan brought Postgres in for the usage ledger in Phase 3. Week 6 needs a database earlier: tenants
and API keys (ADR 0010) and Keycloak's own data. The cluster has two small nodes; only node-2 has a
real data disk (ADR 0006).

## Options considered
1. **Keycloak's embedded dev database, keys in a ConfigMap:** no database to run, but neither is fit for
   anything beyond a demo, and it's all thrown away in week 10.
2. **A Postgres operator** (such as CloudNativePG): failover, backups and upgrades handled, but a new
   dependency, CRDs and more memory on small nodes.
3. **One Postgres StatefulSet from the official image:** simple and transparent; backups and failover
   are left to us.

## Decision
Option 3, now: Postgres 18.6 (official image, pinned by digest) as a one-replica StatefulSet on node-2's
data disk through a static local PV, running as the image's `postgres` user (999).
- **Separation:** one database and one login per application (`tollgate`, `keycloak`), created by an
  init script on first start, so neither application can read the other's tables. The superuser
  password is used for nothing else.
- **Schema:** SQLAlchemy models plus Alembic migrations packaged inside the gateway, applied by a
  Kubernetes Job (`deploy/gateway/migrate-job.yaml`) once per release. Not by the gateway pods: two
  replicas migrating at once would race.
- **Secrets:** all passwords are made by hand as Kubernetes Secrets, never in Git.

## Consequences
- Small footprint: about 80 MiB of RAM in use.
- **No high availability:** a Postgres restart or a node-2 outage stops new API-key checks (503) once the
  gateway's 60 s cache runs out, and stops Keycloak issuing tokens. Week 16's chaos drills will measure
  that.
- **No backups yet.** The PV is `Retain`, so deleting the StatefulSet keeps the data, but there's no
  off-node copy. Before real tenants (week 20): scheduled `pg_dump` to somewhere other than node-2.
- The ledger (Phase 3) will go in the same instance, in the `tollgate` database, as new tables and
  migrations.
- Revisit for Stage 3 (a client's infrastructure), where a managed Postgres or an operator with failover
  is the right default.
