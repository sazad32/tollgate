"""The gateway's own metrics (ADR 0019), served at /metrics for Prometheus.

What they answer under load:
- How many requests are in flight, and how long each takes, to its first byte and to its last.
- Where the time before the model goes: checking the key (`auth`), reserving tokens in Redis
  (`rate_limit`), waiting for a fair turn (`queue`), and the model server's response headers
  (`upstream_headers`).
- Whether the event loop is keeping up. Every coroutine shares one thread, so one blocking call
  delays every request at once. `event_loop_lag` measures how late a short sleep wakes up.

.NET: like System.Diagnostics.Metrics instruments exported by OpenTelemetry's Prometheus exporter.
Each app gets its own registry, so tests can build many apps in one process.
"""

import asyncio
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tollgate_gateway.errors import openai_error

log = logging.getLogger("tollgate.gateway.metrics")

# Seconds. Stages are short (Redis, a cached key), requests long (a whole stream).
STAGE_BUCKETS = (0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5)
REQUEST_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)
LAG_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5)


class GatewayMetrics:
    def __init__(self) -> None:
        r = self.registry = CollectorRegistry()
        self.in_flight = Gauge("tollgate_gateway_requests_in_flight",
                               "API requests being answered, streams until their last byte",
                               registry=r)  # fmt: skip
        self.requests = Counter("tollgate_gateway_requests", "API requests answered, by status",
                                ["status"], registry=r)  # fmt: skip
        self.first_byte = Histogram(
            "tollgate_gateway_time_to_first_byte_seconds",
            "Request received to the first body byte sent (a stream's first event)",
            buckets=REQUEST_BUCKETS,
            registry=r,
        )
        self.duration = Histogram("tollgate_gateway_request_seconds",
                                  "Request received to its last byte sent",
                                  buckets=REQUEST_BUCKETS, registry=r)  # fmt: skip
        self.stage = Histogram("tollgate_gateway_stage_seconds",
                               "Time in each step before the model answers", ["stage"],
                               buckets=STAGE_BUCKETS, registry=r)  # fmt: skip
        self.loop_lag = Histogram("tollgate_gateway_event_loop_lag_seconds",
                                  "How late a short sleep on the event loop woke up",
                                  buckets=LAG_BUCKETS, registry=r)  # fmt: skip
        self.usage_buffered = Gauge("tollgate_gateway_usage_buffered",
                                    "Usage events waiting in memory to be written to Redis",
                                    registry=r)  # fmt: skip
        self.fail_open = Counter("tollgate_gateway_redis_fail_open",
                                 "Times a step went ahead without Redis (rate limit, queue)",
                                 ["step"], registry=r)  # fmt: skip
        self.loop_lag_max_s = 0.0

    @contextmanager
    def timed(self, stage: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.stage.labels(stage=stage).observe(time.perf_counter() - start)

    def render(self) -> bytes:
        return generate_latest(self.registry)


class MetricsMiddleware:
    """Counts and times every /v1 request, streams included: a stream is in flight until its last
    byte is sent, or until it fails or the client leaves.

    A plain ASGI middleware, like RequestIdMiddleware, so streaming isn't buffered or delayed.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/v1/"):
            await self.app(scope, receive, send)
            return
        metrics: GatewayMetrics = scope["app"].state.metrics
        start = time.perf_counter()
        status, first_byte_seen = None, False

        async def timed_send(message: Message) -> None:
            nonlocal status, first_byte_seen
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body" and not first_byte_seen:
                first_byte_seen = True
                metrics.first_byte.observe(time.perf_counter() - start)
            await send(message)

        metrics.in_flight.inc()
        try:
            await self.app(scope, receive, timed_send)
        finally:
            metrics.in_flight.dec()
            metrics.duration.observe(time.perf_counter() - start)
            metrics.requests.labels(status=str(status or 500)).inc()


class InFlightLimit:
    """Backpressure (ADR 0020): at most `max_in_flight` model requests at once on this replica.
    One more is refused at once with 503 and Retry-After, cheaply, before any work is done for
    it. Without a cap, requests piled up until the pod ran out of memory and dropped every
    connection at once (week 14). Health checks and /metrics never count.

    .NET: like ASP.NET Core's ConcurrencyLimiter middleware with a queue of 0.
    """

    PATHS = ("/v1/chat/completions",)

    def __init__(self, app: ASGIApp, max_in_flight: int | None = None) -> None:
        self.app = app
        self.max_in_flight = max_in_flight
        self.in_flight = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (self.max_in_flight is None or scope["type"] != "http"
                or scope["path"] not in self.PATHS):  # fmt: skip
            await self.app(scope, receive, send)
            return
        if self.in_flight >= self.max_in_flight:
            refused = openai_error(503, "The gateway is at capacity. Try again shortly.",
                                   "server_error", "overloaded")  # fmt: skip
            refused.headers["Retry-After"] = "1"
            await refused(scope, receive, send)
            return
        self.in_flight += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self.in_flight -= 1


class LoopLagMonitor:
    """Sleeps for a short interval, again and again, and records how late it woke up. On an idle
    loop that's well under a millisecond; a blocking call, or more work than one core can do, shows
    up here first. Lag over 100 ms is also logged, at most once a minute."""

    def __init__(self, metrics: GatewayMetrics, interval_s: float = 0.25) -> None:
        self.metrics = metrics
        self.interval_s = interval_s
        self._last_warning = 0.0

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            start = loop.time()
            await asyncio.sleep(self.interval_s)
            lag = max(0.0, loop.time() - start - self.interval_s)
            self.metrics.loop_lag.observe(lag)
            self.metrics.loop_lag_max_s = max(self.metrics.loop_lag_max_s, lag)
            if lag > 0.1 and loop.time() - self._last_warning > 60:
                self._last_warning = loop.time()
                log.warning("event loop %.0f ms late: something blocked it, or it's overloaded",
                            lag * 1000)  # fmt: skip
