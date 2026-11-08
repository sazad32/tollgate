"""Reading usage events (schema version 1, docs/usage-events.md). Anything billing can't trust is
an InvalidEventError with a reason, never a guess."""

from datetime import UTC, datetime

import pytest
from tollgate_billing.events import InvalidEventError, parse, raw_fields

REQUEST_ID = "9f2c4b7e1d3a5c6b8e0f1a2b3c4d5e6f"  # conftest's (test modules can't import conftest)


def test_a_valid_event_is_read(event_fields) -> None:
    event = parse(event_fields())
    assert event.idempotency_key == f"{REQUEST_ID}:usage"
    assert event.request_id == REQUEST_ID
    assert (event.tenant_id, event.credential) == ("support", "key:4d1a")
    assert (event.model, event.backend) == ("smollm3-3b", "llama-server-0")
    assert (event.input_tokens, event.output_tokens) == (74, 60)
    assert (event.token_source, event.status, event.stream) == ("reported", "completed", True)
    assert event.started_at == datetime(2026, 11, 8, 17, 22, 4, 519000, tzinfo=UTC)
    assert event.finished_at == datetime(2026, 11, 8, 17, 22, 6, 2000, tzinfo=UTC)
    assert event.version == 1


def test_redis_bytes_are_read_like_strings(event_fields) -> None:
    raw = {k.encode(): v.encode() for k, v in event_fields().items()}
    assert parse(raw) == parse(event_fields())


def test_fields_added_later_are_ignored(event_fields) -> None:
    # A new field keeps the schema version (docs/usage-events.md), so an old reader must cope.
    assert parse(event_fields(region="eu")) == parse(event_fields())


def test_stream_false_is_read(event_fields) -> None:
    assert parse(event_fields(stream="false")).stream is False


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"v": "2"}, "unsupported schema version"),
        ({"v": ""}, "unsupported schema version"),
        ({"idempotency_key": "something-else"}, "idempotency_key"),
        ({"request_id": "short", "idempotency_key": "short:usage"}, "request_id"),
        ({"request_id": "Z" * 32}, "request_id"),
        ({"tenant_id": ""}, "tenant_id"),
        ({"tenant_id": "x" * 65}, "tenant_id"),
        ({"credential": "key:\x00"}, "credential"),
        ({"model": "smol\nlm"}, "model"),
        ({"backend": "b" * 65}, "backend"),
        ({"input_tokens": "-1"}, "input_tokens"),
        ({"input_tokens": "7.5"}, "input_tokens"),
        ({"input_tokens": ""}, "input_tokens"),
        (
            {"output_tokens": "١٢"},
            "output_tokens",
        ),  # Arabic-Indic digits: int() takes them, we don't
        ({"output_tokens": "2147483648"}, "output_tokens"),
        ({"token_source": "guessed"}, "token_source"),
        ({"status": "cancelled"}, "status"),
        ({"stream": "yes"}, "stream"),
        ({"started_at": "yesterday"}, "started_at"),
        ({"finished_at": "2026-11-08T17:22:06"}, "finished_at"),  # no time zone
    ],
)
def test_an_event_billing_cant_trust_is_invalid(event_fields, overrides, reason) -> None:
    with pytest.raises(InvalidEventError, match=reason):
        parse(event_fields(**overrides))


@pytest.mark.parametrize(
    "missing", ["v", "idempotency_key", "tenant_id", "input_tokens", "status", "finished_at"]
)
def test_a_missing_field_is_invalid(event_fields, missing) -> None:
    fields = event_fields()
    del fields[missing]
    with pytest.raises(InvalidEventError, match=f"missing field '{missing}'"):
        parse(fields)


def test_bytes_that_arent_utf8_are_invalid(event_fields) -> None:
    raw = {k.encode(): v.encode() for k, v in event_fields().items()}
    raw[b"model"] = b"\xff\xfe"
    with pytest.raises(InvalidEventError, match="not UTF-8"):
        parse(raw)


def test_raw_fields_keep_undecodable_bytes_visible() -> None:
    # For the rejects table: what arrived, readable, even when it isn't valid UTF-8.
    assert raw_fields({b"model": b"\xffsmol", "v": "1"}) == {"model": "\\xffsmol", "v": "1"}
