# 0018 — Reconciliation: the ledger against the model servers' own counters, no guessed corrections

- Status: accepted
- Date: 2026-11-08
- Amends: ADR 0016 (estimated rows are measured, not corrected)

## Context
The ledger counts each request exactly once (ADR 0016), and invoices match the ledger (ADR 0017).
But that only shows billing agrees with itself: the ledger holds what the gateway reported. Billing
needs an independent check that the models generated what we bill. Three things could make them
differ:
- **Lost usage events:** a gateway pod that crashes while Redis is down (ADR 0015).
- **Requests that skip the gateway:** benchmarks, or a port-forward straight to a model server.
- **Estimates:** a request the client hung up on, or whose stream broke, has no usage from the
  server; the gateway estimates it.

**What the servers actually count**, measured first (2026-11-08). One request at a time through the
gateway, with llama-server's counters read before and after:

| Request | Client saw | Ledger | Server counter |
| --- | --- | --- | --- |
| Completed (×3) | 75 in, 40 out | 75 in, 40 out | 40 out; **16, then 1** in |
| Hung up after 5 chunks | 5 chunks | 112 in, 19 out (estimated) | 21 out; 1 in |

- Output tokens match exactly.
- The prompt counter counts only prompt tokens the server *processed*. Ours were nearly all
  served from its prompt cache: 1 processed of 75.
- A hang-up generates a few tokens past what the gateway relayed: they were in flight when the
  gateway cancelled.

## Options considered
1. **An independent source:**
   - the gateway's logs: not independent;
   - the model servers' logs: unstructured;
   - **the servers' token counters in Prometheus**: already scraped every 15 s and kept 15 days;
   - Anthropic's usage API, for Claude: needs an organization Admin key.
2. **Reading a counter over a day:**
   - PromQL's `increase()`. It extrapolates to the window's edges, drops a new series' first
     value, and returns fractions.
   - **Raw samples.** Count from the last sample at or before the start to the last inside the
     window, and sum the ledger over exactly that span.
3. **Which meters:**
   - input and output;
   - **output only:** llama-server's prompt counter skips cached tokens.
4. **What to do with a gap:**
   - write correction rows to the ledger automatically;
   - **report it and alert, with no automatic corrections.**
5. **How to alert:**
   - a new metric and alert rule, which needs a Pushgateway or an exporter;
   - **fail the Job**, so kube-prometheus-stack's built-in `KubeJobFailed` alert fires.

## Decision
- **`python -m tollgate_billing reconcile`.** A CronJob runs it at 02:00 UTC for yesterday.
  - It stores one row per (day, backend, meter) in `reconciliations`. Running a day again replaces
    its rows: this is a measurement, not money.
  - With `--start`/`--end` it checks any span and stores nothing.
- **Backends:** each local backend in `TOLLGATE_BILLING_RECONCILE_BACKENDS`. Each entry gives the
  gateway's name for it (the ledger's `backend` column), its kind (llama or vllm, which picks the
  counter), and its Prometheus `backend` label if that differs: vLLM's is `vllm-workstation`.
- **The counter, from raw samples:**
  - It counts from the last sample at or before the window's start.
  - A counter that goes down was restarted, and counts again from zero.
  - A series that first appears inside the window (a new pod) counts from its first sample. A new
    server is scraped before the gateway routes to it, so this loses nothing.
  - A replaced pod is a second series, and both count.
- **The ledger:** the *measured* output tokens of rows that finished on that backend within the
  exact span the counter's samples cover.
- **The verdict:**
  - `exact`;
  - `within tolerance`;
  - `gap`, which fails the Job;
  - `no data`: the ledger has rows, but the server has no samples;
  - `idle`.

  By default only estimated tokens may be off, by up to half of them. A fixed or proportional
  slack can be set, and is 0 by default.
- **What a gap means:**
  - The server generated more: requests that skipped the gateway, or lost events.
  - The ledger has more: a bug.
- **Not compared:**
  - input tokens, for the reason above;
  - the hosted model. An Admin key is a far stronger credential than this check is worth, and
    Claude's ledger rows already carry Anthropic's own counts.
- **No automatic corrections. This amends ADR 0016.**
  - A counter is a total over many requests: it can't say which request was off, or by how much.
  - The prompt isn't stored, so nothing can count it again.
  - A correction would be a guess spread across tenants. Instead, the reconciliation measures how
    far estimates drift; if that matters, the estimate gets better.
- **The request audit**, for the Phase 3 gate:
  - `python -m tollgate_bench metering` sends a request mix through the gateway. It records what
    each client saw, keyed by the gateway's `X-Request-ID`.
  - `python -m tollgate_billing check-requests` checks each record against its ledger row.

## Consequences
- **The Phase 3 gate run** (2026-11-08, 21:09 to 21:12 UTC): 200 requests, 100 each from `support`
  and `billing`. Half were plain, four in ten streamed, and one in ten hung up on after 5 chunks.

  | Check | Result |
  | --- | --- |
  | Each request against its ledger row | All 200 found. 180 completed, billed exactly the usage the client saw. 20 hung up, billed their estimate. |
  | Ledger rows | 100 per tenant, 100 distinct keys each: none lost, none twice |
  | Ledger vs invoice preview | Equal for both tenants (support 10,539 in / 3,419 out; billing 9,410 / 3,047) |
  | Ledger vs server counters, output | Ledger 6,092 (5,760 reported = 180 × 32, plus 332 estimated). Servers 6,123: +31, within the 165 allowed. |
- **Hang-up estimates run about 8.5% low.** The 20 hang-ups generated 363 tokens and were
  estimated at 332, so a tenant pays slightly less for a hang-up than the GPU did. A better
  estimate would keep counting until the server confirms the cancel; not now.
- **The first night's run (8 November) will fail, correctly.** The servers generated about 10,700
  tokens between 01:00 and 05:00 UTC, before usage events existed. Since the first event (17:20
  UTC), the ledger and the servers agree.
- **Benchmarks and port-forwards** straight to a model server make that night fail too. That's
  deliberate: it is unmetered GPU use. In Phase 4, a NetworkPolicy that lets only the gateway reach
  the model servers would leave lost events as the only cause of a gap.
- **The window's edges:**
  - A request the server finished just before a scrape, but the gateway recorded just after,
    lands on the other side. That's rare at midnight; run the day again.
  - If a server stops being scraped while it keeps serving, its requests after the last sample
    aren't checked that day.
- **vLLM is not yet checked live.** Its counter's name comes from our dashboards, and whether its
  prompt counter includes cached tokens hasn't been measured. Measure it the next time vLLM runs.
