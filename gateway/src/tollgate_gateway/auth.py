"""Who is calling: turn an Authorization header into a tenant (ADR 0010).

.NET: an AuthenticationHandler. `require_principal` is the FastAPI dependency the /v1 routes use,
like [Authorize] on a controller.
Gotcha: FastAPI has no authentication middleware pipeline; a dependency on the router does the job,
and an exception handler (main.py) turns AuthError into the response.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from fastapi import Request

from tollgate_gateway.jwt_auth import JwtValidator, KeysUnavailableError, TokenError
from tollgate_gateway.keys import hash_key, looks_like_key
from tollgate_gateway.tenants import AuthStore, Tenant

log = logging.getLogger("tollgate.gateway.auth")


class AuthError(Exception):
    """A refused request: status (401, 403 or 503), an OpenAI-style error code, a message."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def unavailable() -> AuthError:
    """A new error each time: an exception object carries its own traceback, so never share one."""
    return AuthError(503, "auth_unavailable", "Authentication is temporarily unavailable.")


@dataclass(frozen=True)
class Principal:
    """The authenticated caller, kept on the request for limits, routing and billing."""

    tenant: Tenant
    method: Literal["api_key", "jwt"]
    key_id: str | None = None  # which API key, when method == "api_key"
    subject: str | None = None  # the token's `sub`, when method == "jwt"


class Authenticator:
    """Checks credentials against the store, with a short cache so most requests skip the database.

    Tradeoff: a revoked key or disabled tenant keeps working until its cache entry expires
    (`cache_ttl_s`, 60 s by default). Unknown keys aren't cached, so they always hit the database;
    an ingress rate limit (week 17) keeps that from being abused.
    """

    def __init__(
        self,
        store: AuthStore,
        pepper: bytes,
        jwt: JwtValidator | None = None,
        cache_ttl_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.store = store
        self.pepper = pepper
        self.jwt = jwt
        self.cache_ttl_s = cache_ttl_s
        self.clock = clock
        # Keyed by the key's hash, never the key itself.
        self._keys: dict[str, tuple[Principal, float]] = {}
        self._tenants: dict[str, tuple[Tenant | None, float]] = {}

    async def authenticate(self, authorization: str | None) -> Principal:
        token = self._bearer(authorization)
        principal = await (self._by_key(token) if looks_like_key(token) else self._by_jwt(token))
        if principal.tenant.disabled:
            raise AuthError(403, "tenant_disabled", "This tenant is disabled.")
        return principal

    @staticmethod
    def _bearer(authorization: str | None) -> str:
        if not authorization:
            raise AuthError(
                401,
                "missing_credentials",
                "Missing credentials. Send 'Authorization: Bearer <API key or token>'.",
            )
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() != "bearer" or not value.strip():
            raise AuthError(401, "invalid_credentials", "Use 'Authorization: Bearer <credential>'.")
        return value.strip()

    async def _by_key(self, key: str) -> Principal:
        key_hash = hash_key(key, self.pepper)
        now = self.clock()
        cached = self._keys.get(key_hash)
        if cached and cached[1] > now:
            return cached[0]
        try:
            found = await self.store.key_by_hash(key_hash)
        except Exception as exc:  # any store failure is our outage, never the caller's mistake
            log.warning("auth store failed: %r", exc)
            raise unavailable() from exc
        if found is None or found[0].revoked_at is not None:
            self._keys.pop(key_hash, None)
            raise AuthError(401, "invalid_api_key", "Invalid API key.")
        record, tenant = found
        principal = Principal(tenant=tenant, method="api_key", key_id=record.id)
        self._keys[key_hash] = (principal, now + self.cache_ttl_s)
        if not tenant.disabled:
            # "Last used" means accepted. Recorded on cache misses only (about once a minute per
            # key), and never allowed to fail the request: it's bookkeeping.
            try:
                await self.store.mark_used(record.id)
            except Exception as exc:
                log.warning("could not record key use: %r", exc)
        return principal

    async def _by_jwt(self, token: str) -> Principal:
        if self.jwt is None:
            raise AuthError(401, "invalid_token", "Bearer tokens are not accepted here.")
        try:
            tenant_id, subject = await self.jwt.tenant_id(token)
        except KeysUnavailableError as exc:
            log.warning("identity provider unavailable: %s", exc)
            raise unavailable() from exc
        except TokenError as exc:
            log.info("token refused: %s", exc)
            raise AuthError(401, "invalid_token", "Invalid or expired token.") from exc
        tenant = await self._tenant(tenant_id)
        if tenant is None:
            raise AuthError(403, "unknown_tenant", "This token's tenant is not registered.")
        return Principal(tenant=tenant, method="jwt", subject=subject)

    async def _tenant(self, tenant_id: str) -> Tenant | None:
        now = self.clock()
        cached = self._tenants.get(tenant_id)
        if cached and cached[1] > now:
            return cached[0]
        try:
            tenant = await self.store.tenant(tenant_id)
        except Exception as exc:
            log.warning("auth store failed: %r", exc)
            raise unavailable() from exc
        self._tenants[tenant_id] = (tenant, now + self.cache_ttl_s)
        return tenant


async def require_principal(request: Request) -> Principal:
    """FastAPI dependency for every /v1 route; also stores the principal on request.state."""
    principal = await request.app.state.authenticator.authenticate(
        request.headers.get("authorization")
    )
    request.state.principal = principal
    return principal
