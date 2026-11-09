"""The gateway's own metrics (ADR 0019): what Prometheus scrapes to see where time goes."""

import asyncio
import time

import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families
from tollgate_gateway.metrics import GatewayMetrics, LoopLagMonitor, MetricsMiddleware

pytestmark = pytest.mark.asyncio


def sample(text: str, name: str, **labels: str) -> float | None:
    """The value of one sample in Prometheus text, or None if it isn't there."""
    for family in text_string_to_metric_families(text):
        for s in family.samples:
            if s.name == name and all(s.labels.get(k) == v for k, v in labels.items()):
                return s.value
    return None


async def scrape(http: httpx.AsyncClient) -> str:
    response = await http.get("/metrics")
    assert response.status_code == 200
    return response.text


async def test_metrics_need_no_api_key(anonymous) -> None:
    response = await anonymous.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert sample(response.text, "tollgate_gateway_requests_in_flight") == 0


async def test_a_completed_request_is_counted_and_timed(client, chat) -> None:
    assert (await client.post("/v1/chat/completions", json=chat())).status_code == 200

    text = await scrape(client)
    assert sample(text, "tollgate_gateway_requests_total", status="200") == 1
    assert sample(text, "tollgate_gateway_request_seconds_count") == 1
    assert sample(text, "tollgate_gateway_time_to_first_byte_seconds_count") == 1
    assert sample(text, "tollgate_gateway_requests_in_flight") == 0
    for stage in ("auth", "rate_limit", "queue", "upstream_headers"):
        assert sample(text, "tollgate_gateway_stage_seconds_count", stage=stage) == 1, stage


async def test_a_refused_request_is_counted_with_its_status(anonymous, chat) -> None:
    assert (await anonymous.post("/v1/chat/completions", json=chat())).status_code == 401

    text = await scrape(anonymous)
    assert sample(text, "tollgate_gateway_requests_total", status="401") == 1
    assert sample(text, "tollgate_gateway_requests_total", status="200") is None


async def test_scrapes_and_health_checks_are_not_counted(anonymous) -> None:
    await anonymous.get("/healthz")
    await anonymous.get("/metrics")

    assert sample(await scrape(anonymous), "tollgate_gateway_request_seconds_count") == 0


async def test_the_usage_buffer_is_visible(client, chat) -> None:
    text = await scrape(client)

    assert sample(text, "tollgate_gateway_usage_buffered") == 0


# --- the middleware, driven by hand: a stream is in flight until its last byte --------------------


class HandStream:
    """An ASGI app that sends a stream's chunks only when the test says so."""

    def __init__(self, fail: bool = False) -> None:
        self.go = asyncio.Event()
        self.fail = fail

    async def __call__(self, scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"data: 1\n\n", "more_body": True})
        await self.go.wait()
        if self.fail:
            raise RuntimeError("the stream broke")
        await send({"type": "http.response.body", "body": b"", "more_body": False})


class FakeApp:
    def __init__(self, metrics: GatewayMetrics) -> None:
        self.state = type("State", (), {"metrics": metrics})()


async def drive(stream: HandStream, metrics: GatewayMetrics) -> asyncio.Task:
    middleware = MetricsMiddleware(stream)
    scope = {"type": "http", "path": "/v1/chat/completions", "app": FakeApp(metrics)}

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        pass

    return asyncio.create_task(middleware(scope, receive, send))


async def test_a_stream_is_in_flight_until_its_last_byte() -> None:
    metrics = GatewayMetrics()
    stream = HandStream()

    task = await drive(stream, metrics)
    await asyncio.sleep(0.01)
    assert metrics.in_flight._value.get() == 1
    first_byte = sample(
        metrics.render().decode(), "tollgate_gateway_time_to_first_byte_seconds_count"
    )
    assert first_byte == 1

    stream.go.set()
    await task
    assert metrics.in_flight._value.get() == 0
    assert sample(metrics.render().decode(), "tollgate_gateway_request_seconds_count") == 1


async def test_a_stream_that_raises_still_leaves_flight() -> None:
    metrics = GatewayMetrics()
    stream = HandStream(fail=True)

    task = await drive(stream, metrics)
    await asyncio.sleep(0.01)
    stream.go.set()
    with pytest.raises(RuntimeError):
        await task

    assert metrics.in_flight._value.get() == 0
    assert sample(metrics.render().decode(), "tollgate_gateway_requests_total", status="200") == 1


# --- the event loop -------------------------------------------------------------------------------


async def test_a_blocked_event_loop_shows_as_lag() -> None:
    metrics = GatewayMetrics()
    monitor = LoopLagMonitor(metrics, interval_s=0.05)
    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0.12)  # a couple of quiet measurements first

    time.sleep(0.3)  # blocking call: nothing else on the loop can run  # noqa: ASYNC251
    await asyncio.sleep(0.12)
    task.cancel()

    text = metrics.render().decode()
    assert sample(text, "tollgate_gateway_event_loop_lag_seconds_bucket", le="0.1") >= 2
    assert sample(text, "tollgate_gateway_event_loop_lag_seconds_sum") >= 0.2
    assert metrics.loop_lag_max_s >= 0.2


async def test_loop_debug_turns_on_slow_callback_warnings(make_gateway) -> None:
    app = make_gateway(loop_debug=True)

    async with app.router.lifespan_context(app):
        loop = asyncio.get_running_loop()
        assert loop.get_debug()
        assert loop.slow_callback_duration == pytest.approx(0.1)

    assert not asyncio.get_running_loop().get_debug()
