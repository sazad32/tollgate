# 0016 — Billing consumer: a Redis consumer group into an append-only ledger, exactly once by key

- Status: accepted
- Date: 2026-11-08

## Context
The gateway writes one usage event per request a model ran to the Redis stream `tollgate:usage`, at
least once (ADR 0015): a retry can write the same event twice. Billing must turn that into a ledger
where each request counts **exactly once** (hard problem #3), whatever crashes and whenever:
billing itself, Postgres, Redis. It must also decide what a tenant pays for a request that ended
early (hard problem #1): the client hung up, or the stream broke. And it must never touch the API
(ADR 0002): the gateway doesn't wait for it, and it may be down for hours.

## Options considered
1. **What makes it exactly once:**
   - Remember seen keys in Redis (SET NX with a TTL): a second store to keep consistent with the
     ledger, and duplicates after the TTL get through.
   - Store the last stream ID read in Postgres, in the same transaction as the rows: exactly once
     for each stream entry, but not for the gateway's duplicates, which are separate entries.
   - **The ledger's primary key is the idempotency key**, written with `INSERT ... ON CONFLICT DO
     NOTHING`: the check and the write are one statement, and it never expires.
2. **How to read the stream:** plain `XREAD` from an offset billing stores itself, or **a consumer
   group**, where Redis keeps each consumer's unacknowledged ("pending") entries and lets another
   consumer take them over.
3. **When to acknowledge:** before writing (at most once: a crash loses events) or **after the
   commit** (at least once: a crash repeats a batch, and the key skips it).
4. **An entry billing can't read:** stop the stream until someone fixes it, drop it with a log line,
   or **set it aside in a rejects table** with the reason and carry on.
5. **Partial and cancelled streams:** bill only completed requests; bill everything; or **bill what
   the tenant caused**: completed and client-disconnected requests, not ones that broke on our side.
6. **Trimming the stream:** a length cap (`MAXLEN`, which can delete unbilled events), or **trim only
   what every group has acknowledged and is older than a replay window** (`MINID`).
7. **Where the ledger lives:** the gateway's database, or **billing's own database and login** in
   the same Postgres, as ADR 0011 does for Keycloak.
8. **Testing the SQL:** SQLite as a stand-in, or **a real Postgres**: a service container in CI
   and a throwaway one locally.

## Decision
- **A separate component**, `tollgate_billing`, with its own Deployment, image and database. It
  shares nothing with the gateway but the stream's format (docs/usage-events.md), which a contract
  test checks from both sides.
- **The consumer group `billing` reads the stream.** It starts at the stream's first entry, so events
  written before billing existed are billed too. Each pass:
  1. Read a batch of up to 100. The order is:
     - its own pending entries first (a batch whose write failed);
     - then entries another consumer has left pending for over 60 s (`XAUTOCLAIM`: that pod died);
     - then new ones, waiting up to 5 s.
  2. Write the batch to the ledger in **one transaction**: `INSERT ... ON CONFLICT
     (idempotency_key) DO NOTHING`, plus any rejects.
  3. Acknowledge it (`XACK`) **only after the commit**.
  - A crash between 2 and 3 repeats the batch, and the ledger skips every row. At-least-once
    delivery plus an idempotent write gives exactly once in the ledger.
- **The ledger is append-only.** `usage_ledger`, keyed by `idempotency_key`, holds every event field,
  the stream entry it came from, and two sets of numbers:
  - the **measured** tokens (the cost to us);
  - the **billed** tokens (what the tenant pays). Check constraints keep them within the measured.
  - A correction (week 13) will be a new row, never an edit.
- **The billing rule** (`policy.py`), applied when the event is written:

  | Status | Billed |
  | --- | --- |
  | `completed` | All tokens, reported or estimated |
  | `client_disconnected` | The estimate. The GPU did the work, and otherwise a client could read the whole answer, hang up before the usage chunk and pay nothing. |
  | `failed` | Nothing: the stream broke on our side. |
- **Untrusted entries go to `usage_rejects`** and are acknowledged. That covers:
  - an unknown schema version;
  - a missing or malformed field;
  - text Postgres can't hold (NUL characters);
  - a value outside a column's range.

  The reader checks everything Postgres would refuse, so a bad entry can never make a batch fail
  forever. Any other database error is treated as an outage: the batch is retried every 5 s,
  never skipped.
- **Trim with `XTRIM MINID`** once a minute. An entry goes only when:
  - every consumer group has acknowledged it;
  - and it's older than **24 hours**, so a day of events can be replayed into a rebuilt ledger.

  Unbilled entries are never trimmed, however old. Consumers left by old pods are forgotten once
  they owe nothing and have been idle for an hour.
- **Liveness:** the loop touches `/tmp/alive` on every pass, and the probe restarts a pod whose file
  is over a minute old. An outage doesn't stop the loop, it only makes it wait, so the pod isn't
  restarted for one.

## Consequences
- **What survives what:**

  | Failure | What happens |
  | --- | --- |
  | Billing is down | Events wait in the stream (256 MB holds about 600,000). The API doesn't notice. |
  | Postgres is down | Nothing is acknowledged; billing retries. |
  | Redis is down | The gateway buffers (ADR 0015), billing retries. |
  | A billing pod dies mid-batch | Its successor takes the batch over after a minute. |
- **Duplicates cost nothing,** wherever they come from: a gateway retry, a repeated batch, two
  consumers racing, or a deliberate replay (`XGROUP SETID tollgate:usage billing 0`). The tests show
  each one, and racing writers are tested on Postgres itself.
- **Checked on the cluster** (2026-11-08):

  | Check | Result |
  | --- | --- |
  | First start | The 5 events left from week 10 billed: 5 rows. |
  | New requests | Plain, stream and Claude were billed within a second. A hang-up after 5 chunks was billed at the estimate (108 in, 18 out). A refused request made no event. |
  | A copy added to the stream | "1 copies skipped". No new row. |
  | A malformed entry | Rejected with its reason (`missing field 'credential'`), acknowledged. |
  | Replay from the start | "10 copies skipped". Still 9 rows and 1 reject; nothing trimmed. |
  | Billing scaled to 0 | The API answered all 3 requests. The 3 events waited (lag 3). |
  | Billing started with Postgres down | It read the 3 events and logged the outage once, keeping them pending. When Postgres was back 25 s later, it billed them, with no restart. |
  | End state | 13 distinct keys in the stream, 13 ledger rows. |
- **Billed is not always exact.** `estimated` rows carry the gateway's estimates: the prompt
  estimate overcounts SmolLM3, and one token per chunk undercounts vLLM. The ledger keeps
  `token_source`, so week 13's reconciliation can find and correct them, with new rows.
- **Failed requests cost us, not the tenant:** their measured tokens are in the ledger, billed at
  zero. That includes Claude, which Anthropic still charges for.
- **Rejects need a person.** A rejected entry is billed at nothing until someone fixes it and adds
  it back to the stream. The status command shows the count.
- **One consumer is enough for now.** At 100 events a transaction, one pod writes far more than the
  cluster's models can generate. A second replica would be safe (the group splits the work, and the
  key skips any overlap) but isn't needed.
- **A new Secret and two one-time Jobs:**
  - `billing-db` (the login's password and URL);
  - `billing-db-init` creates the database and login, idempotently;
  - `billing-migrate` applies the schema.
