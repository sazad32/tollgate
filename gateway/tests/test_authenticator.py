"""The decision logic: which credentials get in, as whom, and what the cache allows."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from tollgate_gateway.auth import Authenticator, AuthError

pytestmark = pytest.mark.asyncio

PEPPER = b"test-pepper"  # same as conftest's (test modules can't import conftest)
REVOKED = "tg_revokedrevokedrevokedrevokedrevokedrevok"
LEGACY = "tg_legacylegacylegacylegacylegacylegacylega"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def authenticator(auth_store, validator, clock) -> Authenticator:
    return Authenticator(auth_store, PEPPER, validator, cache_ttl_s=60, clock=clock)


async def refused(authenticator: Authenticator, header: str | None) -> AuthError:
    with pytest.raises(AuthError) as caught:
        await authenticator.authenticate(header)
    return caught.value


# --- API keys ------------------------------------------------------------------------------------


async def test_valid_key_identifies_its_tenant(authenticator, support_key) -> None:
    principal = await authenticator.authenticate(f"Bearer {support_key}")

    assert principal.tenant.id == "support"
    assert principal.method == "api_key"
    assert principal.key_id == "key-support"


async def test_missing_header_is_401(authenticator) -> None:
    error = await refused(authenticator, None)

    assert (error.status, error.code) == (401, "missing_credentials")


@pytest.mark.parametrize("header", ["Basic dXNlcjpwYXNz", "Bearer", "Bearer   ", "tg_abc"])
async def test_malformed_header_is_401(authenticator, header) -> None:
    assert (await refused(authenticator, header)).status == 401


async def test_unknown_key_is_401(authenticator) -> None:
    error = await refused(authenticator, "Bearer tg_unknownunknownunknownunknownunknownunkno")

    assert (error.status, error.code) == (401, "invalid_api_key")


async def test_revoked_key_is_401(authenticator) -> None:
    assert (await refused(authenticator, f"Bearer {REVOKED}")).code == "invalid_api_key"


async def test_disabled_tenant_is_403(authenticator) -> None:
    error = await refused(authenticator, f"Bearer {LEGACY}")

    assert (error.status, error.code) == (403, "tenant_disabled")


async def test_database_outage_is_503_not_401(authenticator, auth_store, support_key) -> None:
    # A client must not be told its key is wrong when it's our database that's down.
    auth_store.fail = True

    error = await refused(authenticator, f"Bearer {support_key}")

    assert (error.status, error.code) == (503, "auth_unavailable")


async def test_a_checked_key_skips_the_database_until_the_cache_expires(
    authenticator, auth_store, support_key, clock
) -> None:
    for _ in range(5):
        await authenticator.authenticate(f"Bearer {support_key}")
    assert auth_store.calls == 1

    clock.now += 61
    await authenticator.authenticate(f"Bearer {support_key}")
    assert auth_store.calls == 2


async def test_revocation_takes_effect_when_the_cache_expires(
    authenticator, auth_store, support_key, clock
) -> None:
    await authenticator.authenticate(f"Bearer {support_key}")
    record = next(r for r in auth_store.keys.values() if r.id == "key-support")

    auth_store.keys[record.key_hash] = replace(record, revoked_at=datetime.now(UTC))

    await authenticator.authenticate(f"Bearer {support_key}")  # still cached: accepted
    clock.now += 61
    assert (await refused(authenticator, f"Bearer {support_key}")).code == "invalid_api_key"


async def test_cached_keys_are_stored_by_hash_not_by_key(authenticator, support_key) -> None:
    await authenticator.authenticate(f"Bearer {support_key}")

    assert support_key not in repr(authenticator.__dict__)


# --- Keycloak tokens -----------------------------------------------------------------------------


async def test_valid_token_identifies_its_tenant(authenticator, make_token) -> None:
    principal = await authenticator.authenticate(f"Bearer {make_token()}")

    assert principal.tenant.id == "support"
    assert principal.method == "jwt"
    assert principal.subject == "service-account-support"


async def test_bad_token_is_401(authenticator, make_token) -> None:
    error = await refused(authenticator, f"Bearer {make_token(exp_in=-600)}")

    assert (error.status, error.code) == (401, "invalid_token")


async def test_token_for_an_unknown_tenant_is_403(authenticator, make_token) -> None:
    error = await refused(authenticator, f"Bearer {make_token(tenant='nobody')}")

    assert (error.status, error.code) == (403, "unknown_tenant")


async def test_token_for_a_disabled_tenant_is_403(authenticator, make_token) -> None:
    assert (await refused(authenticator, f"Bearer {make_token(tenant='legacy')}")).status == 403


async def test_tokens_are_refused_when_keycloak_is_not_configured(auth_store, make_token) -> None:
    authenticator = Authenticator(auth_store, PEPPER, jwt=None)

    assert (await refused(authenticator, f"Bearer {make_token()}")).status == 401


async def test_keycloak_outage_is_503_not_401(auth_store, make_token) -> None:
    # Same rule as the database: if we can't fetch Keycloak's keys, it's our outage.
    import httpx
    from tollgate_gateway.jwt_auth import JwksCache, JwtValidator

    async def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Keycloak is restarting")

    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as http:
        validator = JwtValidator(
            JwksCache(http, "http://keycloak.test/certs"),
            issuer="http://keycloak.test/realms/tollgate",
            audience="tollgate-gateway",
        )
        authenticator = Authenticator(auth_store, PEPPER, validator)
        error = await refused(authenticator, f"Bearer {make_token()}")

    assert (error.status, error.code) == (503, "auth_unavailable")


async def test_only_accepted_keys_are_marked_as_used(authenticator, auth_store, support_key):
    # "Last used" must mean "successfully used": a revoked key or a disabled tenant's key is
    # looked up in the database too, but must not look active afterwards.
    await authenticator.authenticate(f"Bearer {support_key}")
    for refused_key in (REVOKED, LEGACY):
        with pytest.raises(AuthError):
            await authenticator.authenticate(f"Bearer {refused_key}")

    assert auth_store.used == ["key-support"]
