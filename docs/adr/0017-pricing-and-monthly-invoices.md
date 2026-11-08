# 0017 — Pricing and monthly invoices: a price list in Git, invoiced by the month a row was recorded

- Status: accepted
- Date: 2026-11-08

## Context
The ledger (ADR 0016) holds each request's billed tokens, exactly once. Week 12 turns them into
money: a price per model, and one invoice per tenant per month. Phase 3's gate is "invoices match
metered tokens exactly". So:
- money must never drift, as floating point would;
- a finalized invoice must never change;
- running the monthly job twice, or twice at once, must invoice nothing twice;
- a missing price must never quietly become a price of zero.

## Options considered
1. **Where prices live:**
   - a database table, edited with a CLI;
   - a database migration per price change;
   - **a TOML file in Git**, mounted as a ConfigMap.
2. **What "pricing tiers" means:**
   - by tenant tier: but the tenant's tier is the rate-limit tier, and it lives in the gateway's
     database;
   - one flat rate per model;
   - **graduated volume bands per model**, like tax brackets.
3. **When a price can change:**
   - on any date, which gives a month two prices, splits its lines and blurs its bands;
   - **only on the 1st of a month**.
4. **Which month a ledger row belongs to:**
   - when the request started: a row that arrives late lands in a month already invoiced, which
     must then be reopened or carry adjustment lines;
   - **when billing recorded it**.
5. **Rounding:**
   - round only the total, so the lines don't add up to it;
   - round each request;
   - **round each line once, half-up, and make the total the sum of the lines**.
6. **Making sure finalize misses no row:**
   - wait a grace period after the month ends and hope;
   - **lock the ledger against writers while finalizing**.
7. **Keeping a row off two invoices:**
   - trust the code;
   - **a table keyed by the ledger row** (`invoice_items`).

## Decision
- **The price list is `deploy/billing/prices.toml`.**
  - A price change is a reviewed pull request, and Git history is the audit trail.
  - Each `[[prices]]` entry gives one model's input and output rates per million tokens, from a
    month (`from = "2026-11"`) until the model's next entry.
  - Rates are strings, read into `Decimal` (C#'s `decimal`). A bare TOML number is a binary float
    and is refused.
  - So are:
    - bands out of order;
    - a band past the last without an end;
    - unknown keys;
    - more than 6 decimal places.
  - A test fails if a model the gateway can answer with has no price.
- **Prices for now** (synthetic chargeback rates for the scenario):

  | Model | Input per 1M | Output per 1M |
  | --- | --- | --- |
  | `smollm3-3b` (local) | 0.10 | 0.40 up to 10M a month, 0.30 after |
  | `claude-opus-5-5` (hosted) | 4.40 | 22.00 |

  Claude is Anthropic's list price ($4 / $20, October 2026) plus 10%. It has no volume discount,
  since our cost doesn't fall with volume. The local model's lower band rewards keeping traffic
  local.
- **A month's invoice holds the ledger rows recorded in that month**, like a card statement goes
  by posting date rather than transaction date.
  - `recorded_at` is set by Postgres when billing writes the row, never by the event.
  - So once a month has ended, no row can join it, and its invoices never need to change.
- **Lines and rounding.** There is one line per model, meter (input or output) and band.
  - Each line is computed exactly, then rounded once to the cent, **half-up**. That rounding is
    set explicitly, because `Decimal` defaults to banker's rounding, as C#'s `Math.Round` does.
  - The total is the sum of the lines.
  - Each line copies its unit price and band, so the invoice reads the same after prices change.
- **Tables** in billing's database:
  - `invoices`, keyed by (tenant, month);
  - `invoice_lines`;
  - `invoice_items`, whose primary key is the ledger row's idempotency key. Postgres itself refuses
    to put a row on two invoices.
- **Finalize** (`invoice finalize`), in one transaction:
  1. Refuse a month that hasn't ended.
  2. `LOCK TABLE usage_ledger IN SHARE ROW EXCLUSIVE MODE`.
     - A ledger batch that began before midnight and hasn't committed yet holds rows for the
       month just ended. The lock waits for it, and holds new writes back until the commit (their
       rows are next month's anyway).
     - The lock conflicts with itself, so a second finalize waits, then finds the invoices made.
  3. Skip tenants already invoiced for the month.
  4. Leave a tenant **uninvoiced** if it used a model with no price that month. That tenant's
     invoice is never billed at zero; the command exits 1, so the Job shows as failed. Add the
     price and run it again.
  5. Write the invoice, its lines, and its items.
- **Verify** (`invoice verify`, and after every finalize) re-prices the month from the ledger and
  the price list, and checks:
  - every ledger row recorded in the month is on its tenant's invoice;
  - every item on the month's invoices is that tenant's row from that month;
  - each invoice's requests, tokens, lines and total are exactly what its rows and prices make.
- **A CronJob**, `tollgate-billing-invoices`, finalizes last month at 06:00 UTC on the 1st.
  - It runs one at a time (`concurrencyPolicy: Forbid`).
  - It still runs later that day if the cluster was down at 06:00.
  - The same commands run by hand: `invoice preview` (this month so far, not stored), `show`,
    `list`.

## Consequences
- **Exact by construction, and checked.**
  - Every billed token in the ledger is on exactly one invoice. The primary key forbids two, and
    verify finds none.
  - The tests on a real Postgres include two finalizers racing, and a ledger write still in flight
    when finalize starts.
  - 12 bugs were planted. 11 were caught. The 12th changed nothing a test could see (the loop's
    early exit at a band edge).
- **A request is billed in the month it was recorded.** One that starts at 23:59:59 on the 31st
  and is recorded after midnight goes on the next month's invoice.
  - The same holds for anything late: events the gateway held while Redis was down, a replay after
    a ledger loss, and week 13's corrections, which will be new ledger rows. They all land on the
    current month's invoice, and a closed month never changes.
- **Small amounts round to nothing.** Each line can gain or lose at most half a cent.
  - At today's volume every line is under half a cent: November's preview is USD 0.00 for both
    tenants.
  - Sub-cent amounts are not carried over to the next month.
- **Finalizing pauses ledger writes** for as long as it runs: well under a second at this size.
  The consumer just waits; nothing is lost.
- **Editing a price already used** makes verify fail for the months it priced. That's deliberate:
  add a new version with a later `from` instead.
- **No usage, no invoice.** Billing only knows tenants from the ledger.
- **`invoice_items` gets one row per request.** That's fine at this size; partition it by month if
  it ever isn't.
- **Checked on the cluster** (2026-11-08):

  | Check | Result |
  | --- | --- |
  | Migration 0002 | Applied; the consumer kept billing. The old pod stopped cleanly on SIGTERM. |
  | Preview, November | support 11 requests (882 in, 235 out), engineering 2 (32 in, 27 out): the `status` totals exactly. USD 0.00 each. |
  | The CronJob, run by hand | "nothing to invoice for 2026-10", then verify passed. |
  | Finalize November early | Refused, exit 1, nothing stored. |
  | A bug found | Rates printed as `0.1` and `22`. `Decimal.normalize()` would print 20.00 as `2E+1`. Fixed, with a test. |

  The first real invoices (November) come from the CronJob at 06:00 UTC on 1 December.
