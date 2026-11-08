"""The billing rule: how many of a request's tokens the tenant pays for (ADR 0016).

The ledger keeps both numbers: what the request measured (the cost to us) and what the tenant is
billed. Invoices (week 12) add up the billed ones; reconciliation (week 13) checks the measured.

- completed: everything, reported or estimated.
- client_disconnected: the estimate. The GPU did the work, and otherwise a client could read the
  whole answer, hang up before the usage chunk and pay nothing.
- failed: nothing. The stream broke on our side (a backend died mid-answer), not the tenant's.
"""

from tollgate_billing.events import UsageEvent


def billable(event: UsageEvent) -> tuple[int, int]:
    """(input, output) tokens the tenant pays for."""
    if event.status == "failed":
        return 0, 0
    return event.input_tokens, event.output_tokens
