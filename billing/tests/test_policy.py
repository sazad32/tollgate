"""The billing rule for partial and cancelled streams (ADR 0016): bill the tenant for the work its
request caused, but not for our failures."""

from tollgate_billing.events import parse
from tollgate_billing.policy import billable


def test_a_completed_request_is_billed_what_the_model_reported(event_fields) -> None:
    assert billable(parse(event_fields(input_tokens="74", output_tokens="60"))) == (74, 60)


def test_a_completed_request_without_a_report_is_billed_the_estimate(event_fields) -> None:
    event = parse(event_fields(token_source="estimated", input_tokens="108", output_tokens="5"))
    assert billable(event) == (108, 5)


def test_a_client_that_hung_up_is_billed_the_estimate(event_fields) -> None:
    # The GPU did the work. And otherwise a client could read the whole answer, hang up before the
    # usage chunk, and pay nothing.
    event = parse(
        event_fields(
            status="client_disconnected", token_source="estimated",
            input_tokens="108", output_tokens="19",
        )
    )  # fmt: skip
    assert billable(event) == (108, 19)


def test_a_failed_request_is_not_billed(event_fields) -> None:
    # The stream broke on our side: the tenant pays nothing. The tokens stay in the ledger as cost.
    event = parse(event_fields(status="failed", token_source="estimated", output_tokens="12"))
    assert billable(event) == (0, 0)
    assert (event.input_tokens, event.output_tokens) == (74, 12)
