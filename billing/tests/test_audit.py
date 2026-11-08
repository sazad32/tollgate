"""The request audit (ADR 0018): what each client saw, against its ledger row."""

from tollgate_billing.audit import ClientRecord, LedgerView, audit, read_records

A, B, C, D = (f"{n:032x}" for n in range(1, 5))


def row(request_id: str, *, tenant: str = "support", status: str = "completed",
        source: str = "reported", tokens: tuple[int, int] = (75, 40),
        billed: tuple[int, int] | None = None) -> LedgerView:  # fmt: skip
    billed = tokens if billed is None else billed
    return LedgerView(request_id, tenant, status, source, *tokens, *billed)


def record(request_id: str | None, outcome: str = "completed", *, tenant: str = "support",
           tokens: tuple[int, int] | None = (75, 40), chunks: int = 0) -> ClientRecord:  # fmt: skip
    t = tokens or (None, None)
    return ClientRecord(request_id, tenant, outcome, t[0], t[1], chunks)


def test_a_completed_request_matches_its_row_exactly() -> None:
    result = audit([record(A)], {A: row(A)})

    assert (result.matched, result.problems) == (1, [])


def test_a_row_with_other_tokens_than_the_client_saw_is_a_problem() -> None:
    result = audit([record(A, tokens=(75, 40))], {A: row(A, tokens=(75, 39))})

    assert result.matched == 0
    assert result.problems == [f"{A}: the client saw 75 in, 40 out; the ledger has 75 in, 39 out"]


def test_a_completed_request_with_no_row_is_a_problem() -> None:
    result = audit([record(A)], {})

    assert result.problems == [f"{A}: completed but not in the ledger"]


def test_a_row_for_another_tenant_is_a_problem() -> None:
    result = audit([record(A)], {A: row(A, tenant="billing")})

    assert result.problems == [f"{A}: sent by support but billed to billing"]


def test_a_completed_request_billed_at_less_than_it_measured_is_a_problem() -> None:
    result = audit([record(A)], {A: row(A, billed=(75, 0))})

    assert result.problems == [f"{A}: completed but billed 75 in, 0 out of 75 in, 40 out"]


def test_a_hang_up_is_billed_its_estimate() -> None:
    hang_up = row(B, status="client_disconnected", source="estimated", tokens=(112, 19))

    result = audit([record(B, "hung_up", tokens=None, chunks=5)], {B: hang_up})

    assert (result.hung_up, result.problems) == (1, [])


def test_a_hang_up_that_finished_first_is_billed_in_full() -> None:
    result = audit([record(B, "hung_up", tokens=None, chunks=40)], {B: row(B)})

    assert (result.hung_up, result.problems) == (1, [])


def test_a_hang_up_estimated_below_what_the_client_received_is_a_problem() -> None:
    # The gateway relayed every chunk the client got: each is at least one token.
    hang_up = row(B, status="client_disconnected", source="estimated", tokens=(112, 3))

    result = audit([record(B, "hung_up", tokens=None, chunks=5)], {B: hang_up})

    assert result.problems == [f"{B}: the client received 5 chunks; the ledger has 3 out"]


def test_a_refused_request_has_no_row() -> None:
    result = audit([record(C, "error", tokens=None)], {})

    assert (result.refused, result.problems) == (1, [])


def test_a_refused_request_with_a_billed_row_is_a_problem() -> None:
    result = audit([record(C, "error", tokens=None)], {C: row(C)})

    assert result.problems == [f"{C}: the client got an error but the ledger bills 75 in, 40 out"]


def test_a_request_without_an_id_cannot_be_checked() -> None:
    result = audit([record(None, "error", tokens=None)], {})

    assert (result.unchecked, result.problems) == (1, [])


def test_records_are_read_from_json_lines_and_extra_fields_ignored() -> None:
    lines = [
        f'{{"request_id": "{D}", "tenant": "billing", "outcome": "completed", '
        '"input_tokens": 75, "output_tokens": 40, "chunks": 41, "kind": "stream", "extra": 1}',
        "",
        '{"request_id": null, "tenant": "support", "outcome": "error", "input_tokens": null, '
        '"output_tokens": null, "chunks": 0}',
    ]

    assert read_records(lines) == [
        ClientRecord(D, "billing", "completed", 75, 40, 41),
        ClientRecord(None, "support", "error", None, None, 0),
    ]


def test_a_byte_order_mark_from_a_windows_pipe_is_ignored() -> None:
    # PowerShell 5.1 adds one when it pipes text into a program (kubectl exec -i).
    line = (f'\ufeff{{"request_id": "{A}", "tenant": "support", "outcome": "completed", '
            '"input_tokens": 75, "output_tokens": 40, "chunks": 0}')  # fmt: skip

    assert read_records([line]) == [ClientRecord(A, "support", "completed", 75, 40, 0)]
