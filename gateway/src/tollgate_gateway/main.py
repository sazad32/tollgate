"""The gateway app. Run it with: uvicorn --factory tollgate_gateway.main:create_app

.NET: create_app() is Program.cs: build the app, register its pieces, return it. Uvicorn (the
Kestrel of Python) calls it and runs the result.
Gotcha: nothing is created at import time. The upstream client is made in `lifespan`, Python's
IHostedService.StartAsync/StopAsync: code before `yield` runs at startup, after it at shutdown.
"""

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tollgate_gateway.auth import Authenticator, AuthError
from tollgate_gateway.config import Settings
from tollgate_gateway.db import PostgresAuthStore, make_engine
from tollgate_gateway.errors import openai_error
from tollgate_gateway.jwt_auth import JwksCache, JwtValidator
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
    app.state.upstream = make_client(settings, app.state.upstream_transport)
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
    try:
        yield
    finally:
        await app.state.upstream.aclose()
        await jwks_client.aclose()
        if engine is not None:
            await engine.dispose()


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
    jwks_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the app. Tests pass settings and fakes for the model server, the tenant store and
    Keycloak; Uvicorn passes none of them."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    app = FastAPI(title="Tollgate gateway", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings or Settings()  # type: ignore[call-arg]  (env supplies the rest)
    app.state.upstream_transport = upstream_transport
    app.state.auth_store = auth_store
    app.state.jwks_transport = jwks_transport
    app.include_router(router)
    app.add_exception_handler(AuthError, auth_error_response)
    app.add_middleware(RequestIdMiddleware)
    return app
