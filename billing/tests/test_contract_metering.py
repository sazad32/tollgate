"""The contract between `tollgate_bench metering` and billing's `check-requests` (ADR 0018): every
record the bench writes, the audit reads back with the same values, and knows every outcome.

Like test_contract.py: only this test imports both packages.
"""

import json
from dataclasses import asdict

from tollgate_bench.metering import MeteredRequest
from tollgate_billing.audit import ClientRecord, LedgerView, audit, read_records

ID = "9f2c4b7e1d3a5c6b8e0f1a2b3c4d5e6f"


def test_a_bench_record_reads_back_with_the_same_values() -> None:
    sent = MeteredRequest(ID, "support", "stream", "completed", 200, 90, 32, 32, None)

    [read] = read_records([json.dumps(asdict(sent))])

    assert read == ClientRecord(ID, "support", "completed", 90, 32, 32)


def test_the_audit_knows_every_outcome_the_bench_writes() -> None:
    row = LedgerView(ID, "support", "completed", "reported", 90, 32, 90, 32)
    for outcome, tokens in (("completed", (90, 32)), ("hung_up", (None, None)),
                            ("error", (None, None))):  # fmt: skip
        sent = MeteredRequest(ID, "support", "stream", outcome, 200, *tokens, 5, None)
        result = audit(read_records([json.dumps(asdict(sent))]),
                       {} if outcome == "error" else {ID: row})  # fmt: skip

        assert result.problems == [], outcome
        assert result.matched + result.hung_up + result.refused == 1, outcome
