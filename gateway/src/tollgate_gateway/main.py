"""The gateway app. Run it with: uvicorn --factory tollgate_gateway.main:create_app

.NET: create_app() is Program.cs: build the app, register its pieces, return it. Uvicorn (the
Kestrel of Python) calls it and runs the result.
Gotcha: nothing is created at import time. The upstream client is made in `lifespan`, Python's
IHostedService.StartAsync/StopAsync: code before `yield` runs at startup, after it at shutdown.
"""

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
import httpx2
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tollgate_gateway.admission import Admission
from tollgate_gateway.auth import Authenticator, AuthError
from tollgate_gateway.backends import LoadTracker
from tollgate_gateway.config import Settings
from tollgate_gateway.db import PostgresAuthStore, make_engine
from tollgate_gateway.errors import openai_error
from tollgate_gateway.hosted import make_hosted
from tollgate_gateway.jwt_auth import JwksCache, JwtValidator
from tollgate_gateway.limits import TokenBuckets
from tollgate_gateway.queue import FairQueue
from tollgate_gateway.routes import router
from tollgate_gateway.tenants import AuthStore
from tollgate_gateway.upstream import make_client


class RequestIdMiddleware:
    """Give every request an ID: in request.state, sent upstream, returned as X-Request-ID.

    Generated here, never taken from the client: week 10 builds billing idempotency keys on it.
    A plain ASGI middleware rather than @app.middleware("http"), which wraps the response stream
    and can get in the way of streaming and client-disconnect handling.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [*message.get("headers", []), (b"x-request-id", request_id.encode())]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_id)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    log = logging.getLogger("tollgate.gateway")
    app.state.upstream = make_client(settings, app.state.upstream_transport)

    # Routing (ADR 0013): the local backends' load, read in the background; the hosted model.
    app.state.loads = LoadTracker(settings.backends, app.state.upstream)
    poller = None
    if settings.load_poll_interval_s > 0:
        poller = asyncio.create_task(app.state.loads.run(settings.load_poll_interval_s))
    key = settings.anthropic_api_key
    app.state.hosted = make_hosted(
        api_key=key.get_secret_value() if key else None,
        model=settings.hosted_model,
        effort=settings.hosted_effort,
        default_max_tokens=settings.hosted_max_tokens,
        refusal_fallback=settings.hosted_refusal_fallback,
        timeout_s=settings.hosted_timeout_s,
        max_retries=settings.hosted_max_retries,
        transport=app.state.hosted_transport,
    )
    if app.state.hosted is None:
        log.info("TOLLGATE_ANTHROPIC_API_KEY not set: no hosted model, every request stays local")
    jwks_client = httpx.AsyncClient(transport=app.state.jwks_transport, timeout=5.0)
    engine = None
    store = app.state.auth_store
    if store is None:
        if settings.database_url is None:
            raise RuntimeError(
                "TOLLGATE_DATABASE_URL is required (tenants and API keys live there)"
            )
        engine = make_engine(settings.database_url.get_secret_value())
        store = PostgresAuthStore(engine)
    validator = None
    if settings.oidc_issuer and settings.jwks_url:
        validator = JwtValidator(
            JwksCache(jwks_client, settings.jwks_url),
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience,
        )
    app.state.authenticator = Authenticator(
        store,
        settings.api_key_pepper.get_secret_value().encode(),
        validator,
        cache_ttl_s=settings.auth_cache_ttl_s,
    )

    # Rate limits (ADR 0012). Short Redis timeouts: if Redis is down, fail open within half a second
    # rather than holding every request up.
    redis = app.state.redis_client
    own_redis = False
    if redis is None and settings.redis_url is not None:
        redis = Redis.from_url(
            settings.redis_url.get_secret_value(), socket_timeout=0.5, socket_connect_timeout=0.5
        )
        own_redis = True
    if redis is None:
        log.warning("TOLLGATE_REDIS_URL not set: no rate limits")
    app.state.buckets = TokenBuckets(redis, clock=app.state.clock) if redis is not None else None

    # Fair queuing for the local slots (ADR 0014), in the same Redis.
    app.state.admission = None
    renewals = None
    if settings.fair_queue and redis is not None:
        queue = FairQueue(
            redis,
            clock=app.state.clock,
            lease_s=settings.queue_lease_s,
            max_waiting=settings.queue_max_waiting,
        )
        app.state.admission = Admission(
            queue, app.state.clock, max_wait_s=settings.queue_max_wait_s,
            poll_s=settings.queue_poll_s,
        )  # fmt: skip
        renewals = asyncio.create_task(app.state.admission.run_renewals())
    try:
        yield
    finally:
        for task in (poller, renewals):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if app.state.hosted is not None:
            await app.state.hosted.client.close()
        await app.state.upstream.aclose()
        await jwks_client.aclose()
        if engine is not None:
            await engine.dispose()
        if own_redis:
            await redis.aclose()


async def auth_error_response(request: Request, exc: Exception) -> JSONResponse:
    """Turn an AuthError raised by require_principal into an OpenAI-style error response."""
    assert isinstance(exc, AuthError)
    response = openai_error(exc.status, exc.message, "invalid_request_error", exc.code)
    if exc.status == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    return response


def create_app(
    settings: Settings | None = None,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
    auth_store: AuthStore | None = None,
    hosted_transport: httpx2.AsyncBaseTransport | None = None,
    jwks_transport: httpx.AsyncBaseTransport | None = None,
    redis_client: Redis | None = None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    """Build the app. Tests pass settings and fakes for the model servers, the tenant store,
    Anthropic, Keycloak, Redis and the clock; Uvicorn passes none of them."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs every request at INFO, including each backend's load poll every second. The gateway
    # logs what matters itself: where each request went, and backends going down or coming back.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = FastAPI(title="Tollgate gateway", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings or Settings()  # type: ignore[call-arg]  (env supplies the rest)
    app.state.upstream_transport = upstream_transport
    app.state.hosted_transport = hosted_transport
    app.state.auth_store = auth_store
    app.state.jwks_transport = jwks_transport
    app.state.redis_client = redis_client
    app.state.clock = clock
    app.include_router(router)
    app.add_exception_handler(AuthError, auth_error_response)
    app.add_middleware(RequestIdMiddleware)
    return app
