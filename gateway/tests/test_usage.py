"""Usage events (ADR 0015): what one event holds, how tokens are counted, and how events reach Redis
Streams without the request waiting, including while Redis is down."""

import asyncio
import json
import logging

import fakeredis
import pytest
import pytest_asyncio
from tollgate_gateway.ratelimit import StreamMeter
from tollgate_gateway.usage import (
    RequestUsage,
    Tokens,
    UsageEmitter,
    UsageEvent,
    tokens_from_body,
)

pytestmark = pytest.mark.asyncio

STREAM = "tollgate:usage"


def event(request_id: str = "r1", **changes: object) -> UsageEvent:
    fields: dict = {
        "request_id": request_id,
        "tenant_id": "support",
        "credential": "key:key-support",
        "model": "smollm3-3b",
        "backend": "node-1",
        "input_tokens": 9,
        "output_tokens": 5,
        "token_source": "reported",
        "status": "completed",
        "stream": False,
        "started_at": 1_000_000.0,
        "finished_at": 1_000_002.5,
    }
    fields.update(changes)
    return UsageEvent(**fields)


# --- the event -----------------------------------------------------------------------------------


async def test_the_idempotency_key_is_the_request_id_and_the_event_type() -> None:
    assert event("abc").idempotency_key == "abc:usage"


async def test_an_event_goes_into_redis_as_flat_strings() -> None:
    fields = event().to_fields()

    assert all(isinstance(k, str) and isinstance(v, str) for k, v in fields.items())
    assert fields["v"] == "1"
    assert fields["idempotency_key"] == "r1:usage"
    assert fields["input_tokens"] == "9"
    assert fields["stream"] == "false"
    assert fields["started_at"] == "1970-01-12T13:46:40.000Z"  # UTC, ISO 8601


async def test_an_event_reads_back_from_redis_unchanged() -> None:
    original = event(stream=True, token_source="estimated", status="client_disconnected")
    raw = {k.encode(): v.encode() for k, v in original.to_fields().items()}  # as Redis returns

    assert UsageEvent.from_fields(raw) == original


# --- counting tokens -----------------------------------------------------------------------------


async def test_a_reply_with_a_usage_report_is_reported() -> None:
    body = json.dumps({"usage": {"prompt_tokens": 9, "completion_tokens": 5}}).encode()

    assert tokens_from_body(body, prompt_estimate=100, output_estimate=512) == Tokens(
        9, 5, "reported"
    )


async def test_a_reply_without_one_is_charged_the_estimate() -> None:
    assert tokens_from_body(b"{}", prompt_estimate=100, output_estimate=512) == Tokens(
        100, 512, "estimated"
    )


def sse(*chunks: dict) -> bytes:
    return b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks)


async def test_a_stream_with_a_usage_report_is_reported() -> None:
    meter = StreamMeter(keep_usage=False)
    meter.feed(sse({"choices": [{"delta": {"content": "Hi"}}]}))
    meter.feed(sse({"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2}}))

    assert meter.tokens(prompt_estimate=100) == Tokens(9, 2, "reported")


async def test_a_stream_cut_short_is_the_prompt_estimate_plus_the_chunks_sent() -> None:
    meter = StreamMeter(keep_usage=False)
    meter.feed(sse(*({"choices": [{"delta": {"content": "x"}}]} for _ in range(3))))

    assert meter.tokens(prompt_estimate=100) == Tokens(100, 3, "estimated")
    assert not meter.failed


async def test_an_error_event_in_the_stream_marks_it_failed() -> None:
    meter = StreamMeter(keep_usage=False)
    meter.feed(sse({"error": {"message": "broke", "code": "stream_interrupted"}}))

    assert meter.failed


# --- the emitter ---------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def server():
    return fakeredis.FakeServer()


@pytest_asyncio.fixture
async def redis(server):
    client = fakeredis.FakeAsyncRedis(server=server)
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def emitter(redis):
    emitter = UsageEmitter(redis, stream=STREAM, max_buffered=3, retry_s=0.02)
    task = asyncio.create_task(emitter.run())
    yield emitter
    task.cancel()


async def stored(redis, count: int, wait_s: float = 1.0) -> list[UsageEvent]:
    """The events in the stream, once there are `count` of them."""
    deadline = asyncio.get_running_loop().time() + wait_s
    while True:
        entries = await redis.xrange(STREAM)
        if len(entries) >= count or asyncio.get_running_loop().time() > deadline:
            return [UsageEvent.from_fields(fields) for _, fields in entries]
        await asyncio.sleep(0.01)


async def eventually(check) -> bool:
    """The emitter updates its counters just after Redis answers: give it a moment."""
    for _ in range(100):
        if check():
            return True
        await asyncio.sleep(0.01)
    return check()


async def test_emitting_never_waits_for_redis(emitter) -> None:
    # emit() only appends to memory: a plain function, nothing to await.
    assert emitter.emit(event()) is None


async def test_an_emitted_event_reaches_the_stream(emitter, redis) -> None:
    emitter.emit(event("r1"))
    emitter.emit(event("r2"))

    events = await stored(redis, 2)

    assert [e.request_id for e in events] == ["r1", "r2"]
    assert await eventually(lambda: emitter.sent == 2)


async def test_while_redis_is_down_events_wait_and_arrive_later(emitter, redis, server) -> None:
    server.connected = False
    emitter.emit(event("r1"))
    await asyncio.sleep(0.1)  # a few failed attempts
    assert emitter.pending == 1

    server.connected = True

    assert [e.request_id for e in await stored(redis, 1)] == ["r1"]
    assert await eventually(lambda: emitter.pending == 0)


async def test_a_full_buffer_drops_the_event_and_logs_it_in_full(emitter, server, caplog) -> None:
    server.connected = False
    for i in range(3):
        emitter.emit(event(f"r{i}"))

    with caplog.at_level(logging.ERROR, logger="tollgate.gateway.usage"):
        emitter.emit(event("r-lost"))

    assert emitter.dropped == 1
    assert emitter.pending == 3
    assert '"request_id": "r-lost"' in caplog.text  # recoverable from the logs


async def test_shutdown_sends_what_is_left(redis) -> None:
    emitter = UsageEmitter(redis, stream=STREAM)  # its background loop never ran
    emitter.emit(event("r1"))

    await emitter.flush(timeout_s=1)

    assert [e.request_id for e in await stored(redis, 1)] == ["r1"]


# --- one request's accounting --------------------------------------------------------------------


class FakeReservation:
    def __init__(self) -> None:
        self.settled: list[int] = []

    async def settle(self, actual: int) -> None:
        self.settled.append(actual)


def account(emitter, reservation=None) -> RequestUsage:
    return RequestUsage(
        reservation=reservation,
        emitter=emitter,
        request_id="r1",
        tenant_id="support",
        credential="key:key-support",
        stream=False,
        started_at=1_000_000.0,
        clock=lambda: 1_000_002.5,
    )


async def test_recording_settles_the_bucket_and_emits_one_event(emitter, redis) -> None:
    reservation = FakeReservation()
    usage = account(emitter, reservation)

    await usage.record("smollm3-3b", "node-1", "completed", Tokens(9, 5, "reported"))

    assert reservation.settled == [14]
    assert await stored(redis, 1) == [event()]


async def test_a_request_is_recorded_only_once(emitter, redis) -> None:
    reservation = FakeReservation()
    usage = account(emitter, reservation)

    await usage.record("smollm3-3b", "node-1", "completed", Tokens(9, 5, "reported"))
    await usage.record("smollm3-3b", "node-1", "failed", Tokens(1, 1, "estimated"))
    await usage.refund()

    assert reservation.settled == [14]
    assert len(await stored(redis, 2, wait_s=0.2)) == 1


async def test_a_refund_gives_the_reservation_back_and_emits_nothing(emitter, redis) -> None:
    reservation = FakeReservation()
    usage = account(emitter, reservation)

    await usage.refund()

    assert reservation.settled == [0]
    assert await stored(redis, 1, wait_s=0.2) == []


async def test_without_redis_or_limits_recording_still_works() -> None:
    await account(None, None).record("m", "b", "completed", Tokens(1, 1, "reported"))
