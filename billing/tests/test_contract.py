"""The contract between the gateway and billing (docs/usage-events.md): every event the gateway can
write, billing reads back with the same values.

Billing doesn't import the gateway at runtime (they deploy separately). Only this test uses both, so
a change on either side that breaks the other fails here, before anything ships.
.NET: a consumer-driven contract test, like Pact, without the broker.
"""

from datetime import UTC, datetime

import pytest
from tollgate_billing.events import parse
from tollgate_gateway.usage import UsageEvent as GatewayEvent

STARTED = datetime(2026, 11, 8, 17, 22, 4, 519000, tzinfo=UTC).timestamp()


def gateway_event(**overrides) -> GatewayEvent:
    values = {
        "request_id": "9f2c4b7e1d3a5c6b8e0f1a2b3c4d5e6f",
        "tenant_id": "engineering",
        "credential": "token:service-account-engineering",
        "model": "claude-opus-5-5",
        "backend": "hosted",
        "input_tokens": 16,
        "output_tokens": 16,
        "token_source": "reported",
        "status": "completed",
        "stream": False,
        "started_at": STARTED,
        "finished_at": STARTED + 1.483,
    }
    values.update(overrides)
    return GatewayEvent(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"stream": True, "model": "smollm3-3b", "backend": "vllm"},
        {"status": "client_disconnected", "token_source": "estimated", "stream": True},
        {"status": "failed", "token_source": "estimated", "backend": "llama-server-1"},
        {"credential": "key:4d1a9b", "input_tokens": 0, "output_tokens": 0},
    ],
)
def test_billing_reads_every_event_the_gateway_writes(overrides) -> None:
    sent = gateway_event(**overrides)
    # Through the bytes Redis hands back, as the consumer gets them.
    raw = {k.encode(): v.encode() for k, v in sent.to_fields().items()}
    read = parse(raw)

    assert read.idempotency_key == sent.idempotency_key
    for name in ("request_id", "tenant_id", "credential", "model", "backend", "input_tokens",
                 "output_tokens", "token_source", "status", "stream"):  # fmt: skip
        assert getattr(read, name) == getattr(sent, name), name
    # The stream carries milliseconds.
    assert read.started_at.timestamp() == pytest.approx(sent.started_at, abs=0.001)
    assert read.finished_at.timestamp() == pytest.approx(sent.finished_at, abs=0.001)


def test_the_gateway_writes_the_schema_version_billing_reads() -> None:
    from tollgate_billing.events import SCHEMA_VERSION
    from tollgate_gateway.usage import SCHEMA_VERSION as GATEWAY_VERSION

    assert str(GATEWAY_VERSION) == SCHEMA_VERSION
