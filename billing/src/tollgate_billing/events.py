"""Usage events as billing reads them: schema version 1 (docs/usage-events.md, ADR 0015).

Billing has its own reader instead of importing the gateway's: the two deploy separately, and the
contract between them is the document, checked by tests/test_contract.py.

parse() accepts an event only if every field is one billing can trust; otherwise it raises
InvalidEventError with the reason, and the consumer puts the entry in the rejects table. It checks
everything Postgres would refuse too (lengths, NUL characters, integer range), so a bad event can
never make a whole batch fail to commit.

.NET: a DTO plus a hand-written validator, like a FluentValidation rule set.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

SCHEMA_VERSION = "1"
TOKEN_SOURCES = ("reported", "estimated")
STATUSES = ("completed", "client_disconnected", "failed")
MAX_TOKENS = 2_147_483_647  # Postgres integer

REQUEST_ID = re.compile(r"[0-9a-f]{32}")
DIGITS = re.compile(r"[0-9]{1,10}")  # ASCII only: int() would also take "١٢"
TEXT_LIMITS = {"tenant_id": 64, "credential": 200, "model": 200, "backend": 64}
REQUIRED = ("v", "idempotency_key", "request_id", *TEXT_LIMITS, "input_tokens", "output_tokens",
            "token_source", "status", "stream", "started_at", "finished_at")  # fmt: skip


class InvalidEventError(ValueError):
    """An entry billing can't trust. The message says why."""


@dataclass(frozen=True)
class UsageEvent:
    idempotency_key: str
    request_id: str
    tenant_id: str
    credential: str
    model: str
    backend: str
    input_tokens: int
    output_tokens: int
    token_source: str
    status: str
    stream: bool
    started_at: datetime
    finished_at: datetime
    version: int = 1


def _text(value: bytes | str, what: str) -> str:
    if isinstance(value, str):
        return value
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        raise InvalidEventError(f"{what} is not UTF-8") from None


def raw_fields(fields: Mapping[bytes | str, bytes | str]) -> dict[str, str]:
    """What arrived, as text, for the rejects table: undecodable bytes show as \\xff escapes."""

    def text(value: bytes | str) -> str:
        return value.decode("utf-8", "backslashreplace") if isinstance(value, bytes) else value

    return {text(k): text(v) for k, v in fields.items()}


def _tokens(f: dict[str, str], name: str) -> int:
    value = f[name]
    if not DIGITS.fullmatch(value) or int(value) > MAX_TOKENS:
        raise InvalidEventError(f"{name} must be a whole number, 0 to {MAX_TOKENS}: {value!r}")
    return int(value)


def _time(f: dict[str, str], name: str) -> datetime:
    try:
        value = datetime.fromisoformat(f[name].replace("Z", "+00:00"))
    except ValueError:
        raise InvalidEventError(f"{name} is not an ISO 8601 time: {f[name]!r}") from None
    if value.tzinfo is None:
        raise InvalidEventError(f"{name} has no time zone: {f[name]!r}")
    return value


def _choice(f: dict[str, str], name: str, allowed: tuple[str, ...]) -> str:
    if f[name] not in allowed:
        raise InvalidEventError(f"{name} must be one of {allowed}, got {f[name]!r}")
    return f[name]


def parse(fields: Mapping[bytes | str, bytes | str]) -> UsageEvent:
    """One stream entry's fields, as Redis returns them (bytes) or as strings."""
    f = {_text(k, "a field name"): _text(v, f"field {_text(k, 'a field name')!r}")
         for k, v in fields.items()}  # fmt: skip

    for name in REQUIRED:
        if name not in f:
            raise InvalidEventError(f"missing field '{name}'")

    if f["v"] != SCHEMA_VERSION:
        raise InvalidEventError(f"unsupported schema version {f['v']!r} (reads 1)")
    if not REQUEST_ID.fullmatch(f["request_id"]):
        raise InvalidEventError(f"request_id must be 32 lowercase hex: {f['request_id']!r}")
    if f["idempotency_key"] != f"{f['request_id']}:usage":
        raise InvalidEventError(f"idempotency_key doesn't match request_id: {f['idempotency_key']}")
    for name, limit in TEXT_LIMITS.items():
        # isprintable() is False for NUL (Postgres text can't hold it) and control characters.
        if not 1 <= len(f[name]) <= limit or not f[name].isprintable():
            raise InvalidEventError(f"{name} must be 1 to {limit} printable chars: {f[name]!r}")
    if f["stream"] not in ("true", "false"):
        raise InvalidEventError(f"stream must be 'true' or 'false', got {f['stream']!r}")

    return UsageEvent(
        idempotency_key=f["idempotency_key"],
        request_id=f["request_id"],
        tenant_id=f["tenant_id"],
        credential=f["credential"],
        model=f["model"],
        backend=f["backend"],
        input_tokens=_tokens(f, "input_tokens"),
        output_tokens=_tokens(f, "output_tokens"),
        token_source=_choice(f, "token_source", TOKEN_SOURCES),
        status=_choice(f, "status", STATUSES),
        stream=f["stream"] == "true",
        started_at=_time(f, "started_at"),
        finished_at=_time(f, "finished_at"),
    )
