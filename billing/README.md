# tollgate-billing

Reads usage events from the Redis stream `tollgate:usage` and writes each one, exactly once, to the
usage ledger in Postgres (ADR 0016). The event format is in `docs/usage-events.md`.

| Module | What it does |
| --- | --- |
| `events.py` | Reads one stream entry (schema version 1) or says why it can't be trusted |
| `policy.py` | The billing rule: how many of a request's tokens the tenant pays for |
| `ledger.py` | Writes a batch in one transaction, `ON CONFLICT (idempotency_key) DO NOTHING` |
| `consumer.py` | The consumer group loop: read, write, acknowledge, take over, trim |
| `db.py`, `migrations/` | The `usage_ledger` and `usage_rejects` tables (Alembic) |

## Tests
Most tests need nothing: fake Redis and an in-memory ledger. The ledger's SQL is tested against a
real Postgres, which CI provides; locally, start a throwaway one (Docker):
```powershell
docker run -d --rm --name tollgate-test-pg -e POSTGRES_PASSWORD=test -p 127.0.0.1:54329:5432 postgres:18.6-bookworm
$env:TOLLGATE_TEST_DATABASE_URL = "postgresql+asyncpg://postgres:test@127.0.0.1:54329/postgres"
uv run pytest billing
docker stop tollgate-test-pg   # --rm deletes it
```
Without `TOLLGATE_TEST_DATABASE_URL` those tests are skipped (pytest says so in its summary).
