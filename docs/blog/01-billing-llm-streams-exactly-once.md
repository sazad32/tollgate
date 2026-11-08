# Billing an LLM stream exactly once

*Draft, not published. Tollgate, weeks 10 to 13.*

Tollgate is a self-hosted AI gateway I'm building for a made-up telecom. Its support, billing and
engineering teams share one API. A small open model runs on two old laptop GPUs in a k3s cluster,
and Claude takes the overflow for the one team allowed to use it. Each team is billed for the
tokens it used.

"Billed for the tokens it used" sounds like a counter and a multiplication. Four weeks in, it took:
- a Redis stream;
- a Postgres ledger with a primary key doing most of the work;
- an invoice table keyed by something other than the invoice;
- a nightly job that asks the GPUs what they think happened.

This post walks through the four problems behind that, and what I measured.

## 1. A stream has three endings, and only one comes with a bill

A streamed chat completion sends tokens as they're generated. The token count (`usage`) arrives in
the very last chunk, after the text. So the request can end three ways:

1. **It completes.** The server's usage chunk says exactly how many tokens went in and came out.
2. **The client hangs up halfway.** There's no usage chunk: the server never got to send it.
3. **It breaks on our side.** The connection to the model server drops mid-stream.

For 2 and 3, the gateway estimates:
- input: the prompt's length in characters ÷ 3, plus a margin;
- output: one token per chunk it relayed.

Then the billing rule decides what the tenant pays:

| Ending | Billed |
| --- | --- |
| Completed | Everything the server reported |
| Client hung up | The estimate |
| Broke on our side | Nothing (the tokens are still recorded, as our cost) |

The middle row is the interesting one. If a hang-up were free, a client could read the whole answer
and close the connection just before the usage chunk arrives. The GPU did the work the moment it
generated the tokens, so the tenant pays for them.

**How good is the estimate?** I sent requests through the gateway and read the model server's own
counter before and after each one. A client that hung up after 5 chunks was estimated at 19 output
tokens; the server had generated 21. The gateway cancels the upstream request as soon as it notices
the client is gone, but a couple of tokens are already in flight. Across 20 hang-ups in the final
test run, the estimate came to 332 against 363 generated: about 8.5% low. I've decided I can live
with that, and I know exactly how big it is.

## 2. At least once, then exactly once

The gateway must never wait for billing; a billing outage can't take the API down. So when a request
ends, the gateway appends a usage event to an in-memory buffer and returns. A background loop writes
the buffer to a Redis stream, and keeps retrying while Redis is down.

Retries mean the same event can land in the stream twice. That's fine, as long as it can't be
*billed* twice. Every event carries an idempotency key, `<request id>:usage`, and the request ID is
made by the gateway, never by the client.

The billing consumer reads the stream as a Redis consumer group, then for each batch:
1. writes all of it to Postgres in **one transaction**, with
   `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING`;
2. acknowledges the batch to Redis **only after the commit**.

If billing crashes between the commit and the acknowledgement, Redis hands the batch out again, and
every row is skipped. At-least-once delivery plus an idempotent write gives exactly once in the
ledger. The check and the write are one SQL statement, so there's no window for two consumers to
both decide a row is new. On a real Postgres, five writers racing with the same event produce one
row.

I tried to break it on the cluster:

| What I did | What happened |
| --- | --- |
| Added a copy of an event to the stream | "1 copies skipped", no new row |
| Rewound the consumer group to the start | "10 copies skipped", ledger unchanged |
| Stopped billing, sent 3 requests | The API answered all 3; the events waited |
| Started billing with Postgres down | It read the 3 events and kept them pending; 25 s later, once Postgres was back, it billed them |

One more thing a stream needs is somewhere to put an entry you can't trust. A malformed entry goes
to a rejects table, with the reason, and is acknowledged. Otherwise one bad message blocks the
stream forever, which is a billing outage of its own making.

## 3. Invoices that never change

A month's invoice could hold the requests that *started* that month. But usage arrives late:
- events wait in memory while Redis is down;
- a request starts at 23:59:59 and ends after midnight.

Then a late row lands in a month that's already invoiced, and you're into reopened invoices and
adjustment lines.

Instead, an invoice holds the rows billing *recorded* that month, like a card statement goes by
posting date, not transaction date. Postgres sets `recorded_at` when billing writes the row. Once a
month has ended, nothing can join it.

Two details make that hold:
- **A write still in flight at midnight.** A batch that began at 23:59:59.9 holds rows for the old
  month, and may commit after the invoice job starts. The job first takes a table lock that waits
  for every write in progress, and holds new ones back until it's done (a fraction of a second).
- **Running the job twice.** A separate `invoice_items` table maps each ledger row to its invoice,
  and its primary key is the ledger row. Postgres itself refuses to put a row on two invoices.

Money is `Decimal`, never float, and each line is rounded once to the cent. I set half-up rounding
explicitly, because Python's `Decimal`, like C#'s `Math.Round`, defaults to banker's rounding. Prices
live in a TOML file in Git, as strings, because a bare TOML number is a binary float.

The first invoice preview on the cluster showed a rate of 22.00 as `22`. Python's
`Decimal.normalize()` would print 20.00 as `2E+1`. It's an amusing bug to find in a price list, and
an embarrassing one to send a customer.

## 4. Is the ledger true?

Everything so far proves billing agrees with itself. The ledger holds what the gateway *told* it. Two
things could make that differ from what the GPUs actually did:
- events lost while a gateway pod crashed with Redis down;
- requests that never went through the gateway.

The model servers keep their own counters, which Prometheus already scrapes every 15 seconds. So
every night a job compares, per backend, the ledger's output tokens with the server's count.

Two lessons came from that:
- **Don't use `increase()` for this.** PromQL's `increase()` extrapolates to the edges of the window
  and returns fractions. The job reads raw samples, counts from the last sample before midnight to
  the last one before the next midnight, and sums the ledger over *exactly that span*. It handles
  restarts (the counter drops to zero) and replaced pods (a new series).
- **Measure what the counter counts.** The input counter looked perfect until I read it per
  request: a 75-token prompt added 1. llama-server counts only the prompt tokens it *processed*, and
  it had cached the rest. So the job compares output tokens only; input is checked request by
  request instead (below).

The first real run failed loudly. The servers had generated about 162,000 tokens on the previous day
that the ledger had never seen. That was my own benchmarks and demos from before usage events
existed: some sent straight to the model servers, some through a gateway that didn't meter yet.
That's exactly what the check is for. A gap means
unmetered GPU use or lost events, and the job fails, so the monitoring stack's built-in "job failed"
alert fires.

One thing it deliberately doesn't do is "correct" the ledger. A counter is a daily total across
every tenant: it can't say which request was off. The prompt isn't stored, so nothing can count it
again. Any correction would be a guess spread across tenants. The reconciliation measures the drift
instead. If the drift ever matters, the estimate gets better.

## The gate

The last check for this phase was 200 requests through the gateway: 100 each from two tenants.
Half were plain, 40% streamed, and 10% streamed and abandoned after 5 chunks. Each client recorded
the gateway's request ID and the usage it was told.

| Check | Result |
| --- | --- |
| Each request against its ledger row | All 200 found. The 180 completed requests were billed exactly what the client was told; the 20 hang-ups were billed their estimate. |
| Ledger rows | 100 per tenant, 100 distinct keys each |
| Ledger against the invoice preview | Equal, to the token, for both tenants |
| Ledger against the GPUs' own counters | 6,092 vs 6,123 output tokens. The 31-token difference is the 20 hang-ups. |

At this volume, each tenant's November invoice comes to USD 0.00. Every line is under half a cent,
which is the most honest result in the whole project.

## What I'd still change

- **Hang-ups are underbilled by about 8.5%.** The gateway could keep counting tokens until the
  server confirms the cancel.
- **Nothing stops a client going straight to a model server.** A network policy should let only the
  gateway reach them, so a reconciliation gap could only mean lost events.
- **vLLM's counters haven't been checked live yet.** Its prompt counter may count cached tokens
  differently.

The code is in the Tollgate repo:
- `billing/` (the consumer, ledger, invoices and reconciliation);
- ADRs 0015 to 0018 (the decisions, and the options I didn't take).
