"""Usage events: one per request a model ran, published to Redis Streams for billing (ADR 0015).

The gateway never bills. It records what each request used and hands the record to Redis Streams;
billing (week 11) reads the stream on its own time. So a billing outage can't touch the API.

- RequestUsage: one request's end. It settles the token bucket (ADR 0012) and emits the event, with
  the same numbers, exactly once, whichever way the request ends.
- UsageEmitter: emit() only appends to memory; a background loop writes to Redis. If Redis is down,
  events wait in memory (bounded) and the loop retries. Delivery is at least once: a retry can
  write an event twice, and the idempotency key lets the ledger count it once (week 11).

.NET: UsageEmitter is a bounded Channel<T> drained by a BackgroundService into a message queue.
"""

import asyncio
import json
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from redis.asyncio import Redis

if TYPE_CHECKING:
    from tollgate_gateway.ratelimit import Reservation

log = logging.getLogger("tollgate.gateway.usage")

SCHEMA_VERSION = 1
STREAM = "tollgate:usage"

TokenSource = Literal["reported", "estimated"]
Status = Literal["completed", "client_disconnected", "failed"]


@dataclass(frozen=True)
class Tokens:
    input: int
    output: int
    # reported: the model's own usage report. estimated: the client left (or the stream broke)
    # before the report came, so the gateway counted: prompt estimate + one token per chunk sent.
    source: TokenSource

    @property
    def total(self) -> int:
        return self.input + self.output


def tokens_from_body(body: bytes, prompt_estimate: int, output_estimate: int) -> Tokens:
    """Tokens of a whole (non-streamed) reply: its usage report, else what was reserved for it."""
    try:
        usage = json.loads(body).get("usage") or {}
        prompt, completion = usage["prompt_tokens"], usage["completion_tokens"]
        if isinstance(prompt, int) and isinstance(completion, int):
            return Tokens(prompt, completion, "reported")
    except (ValueError, AttributeError, KeyError, TypeError):
        pass
    return Tokens(prompt_estimate, output_estimate, "estimated")


def _iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


@dataclass(frozen=True)
class UsageEvent:
    """What one request used. Schema version 1; the format is in docs/usage-events.md."""

    request_id: str  # made by the gateway, never by the client (main.py)
    tenant_id: str
    credential: str  # "key:<api key id>" or "token:<subject>"
    model: str  # the model that answered (after an overflow or refusal fallback, not the asked one)
    backend: str  # vllm, llama-server-0, hosted...
    input_tokens: int
    output_tokens: int
    token_source: TokenSource
    status: Status
    stream: bool
    started_at: float  # epoch seconds; ISO 8601 UTC in Redis
    finished_at: float

    @property
    def idempotency_key(self) -> str:
        """The same request always gives the same key, so a duplicate can be recognised."""
        return f"{self.request_id}:usage"

    def to_fields(self) -> dict[str, str]:
        """Flat string fields, as a Redis stream entry holds them."""
        fields = {"v": str(SCHEMA_VERSION), "idempotency_key": self.idempotency_key}
        for name, value in asdict(self).items():
            if name in ("started_at", "finished_at"):
                fields[name] = _iso(value)
            elif isinstance(value, bool):
                fields[name] = "true" if value else "false"
            else:
                fields[name] = str(value)
        return fields

    @classmethod
    def from_fields(cls, raw: dict) -> "UsageEvent":
        f = {
            (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
            for k, v in raw.items()
        }
        return cls(
            request_id=f["request_id"],
            tenant_id=f["tenant_id"],
            credential=f["credential"],
            model=f["model"],
            backend=f["backend"],
            input_tokens=int(f["input_tokens"]),
            output_tokens=int(f["output_tokens"]),
            token_source=f["token_source"],  # type: ignore[arg-type]
            status=f["status"],  # type: ignore[arg-type]
            stream=f["stream"] == "true",
            started_at=_epoch(f["started_at"]),
            finished_at=_epoch(f["finished_at"]),
        )


class UsageEmitter:
    def __init__(
        self,
        redis: Redis,
        *,
        stream: str = STREAM,
        max_buffered: int = 10_000,
        retry_s: float = 5.0,
        batch: int = 100,
    ) -> None:
        self.redis = redis
        self.stream = stream
        self.max_buffered = max_buffered
        self.retry_s = retry_s
        self.batch = batch
        self._buffer: deque[UsageEvent] = deque()
        self._wake = asyncio.Event()
        self.sent = 0
        self.dropped = 0
        self._failing = False

    @property
    def pending(self) -> int:
        return len(self._buffer)

    def emit(self, event: UsageEvent) -> None:
        """Queue an event. Never waits and never raises: billing stays out of the request path."""
        if len(self._buffer) >= self.max_buffered:
            self.dropped += 1
            # Lost to billing, but not to us: the whole event is in the log, to be replayed.
            log.error("usage event dropped, buffer full: %s",
                      json.dumps(event.to_fields(), sort_keys=True))  # fmt: skip
            return
        self._buffer.append(event)
        self._wake.set()

    async def _send_batch(self) -> None:
        events = [self._buffer[i] for i in range(min(self.batch, len(self._buffer)))]
        async with self.redis.pipeline(transaction=False) as pipe:
            for event in events:
                pipe.xadd(self.stream, event.to_fields())  # type: ignore[arg-type]
            await pipe.execute()
        for _ in events:
            self._buffer.popleft()
        self.sent += len(events)

    async def run(self) -> None:
        """Write queued events to Redis until cancelled. On failure, keep them and retry."""
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._buffer:
                try:
                    await self._send_batch()
                except Exception as exc:
                    if not self._failing:
                        log.warning("usage events waiting, Redis unavailable: %r", exc)
                    self._failing = True
                    await asyncio.sleep(self.retry_s)
                    continue
                if self._failing:
                    log.info("usage events flowing again, %d waiting", len(self._buffer))
                    self._failing = False

    async def flush(self, timeout_s: float = 5.0) -> None:
        """At shutdown: one last try to send what's queued."""
        try:
            async with asyncio.timeout(timeout_s):
                while self._buffer:
                    await self._send_batch()
        except Exception as exc:
            for event in self._buffer:
                log.error("usage event lost at shutdown (%r): %s",
                          exc, json.dumps(event.to_fields(), sort_keys=True))  # fmt: skip


class RequestUsage:
    """The end of one request: settle its reservation and emit its usage event, exactly once."""

    def __init__(
        self,
        *,
        reservation: "Reservation | None",
        emitter: UsageEmitter | None,
        request_id: str,
        tenant_id: str,
        credential: str,
        stream: bool,
        started_at: float,
        clock: Callable[[], float],
    ) -> None:
        self.reservation = reservation
        self.emitter = emitter
        self.request_id = request_id
        self.tenant_id = tenant_id
        self.credential = credential
        self.stream = stream
        self.started_at = started_at
        self.clock = clock
        self.done = False

    async def refund(self) -> None:
        """The model never ran (refused, unreachable, rejected): nothing used, no event."""
        if self.done:
            return
        self.done = True
        if self.reservation:
            await self.reservation.settle(0)

    async def record(self, model: str, backend: str, status: Status, tokens: Tokens) -> None:
        """The model ran: charge the real count to the bucket and emit the event."""
        if self.done:
            return
        self.done = True
        if self.reservation:
            await self.reservation.settle(tokens.total)
        if self.emitter is not None:
            self.emitter.emit(
                UsageEvent(
                    request_id=self.request_id,
                    tenant_id=self.tenant_id,
                    credential=self.credential,
                    model=model,
                    backend=backend,
                    input_tokens=tokens.input,
                    output_tokens=tokens.output,
                    token_source=tokens.source,
                    status=status,
                    stream=self.stream,
                    started_at=self.started_at,
                    finished_at=self.clock(),
                )
            )
