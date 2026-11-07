"""Keycloak token validation against a fake JWKS: every way a token can be wrong."""

import jwt
import pytest
from tollgate_gateway.jwt_auth import TokenError

pytestmark = pytest.mark.asyncio


async def test_valid_token_yields_its_tenant_and_subject(validator, make_token) -> None:
    assert await validator.tenant_id(make_token()) == ("support", "service-account-support")


async def test_expired_token_is_refused(validator, make_token) -> None:
    with pytest.raises(TokenError):
        await validator.tenant_id(make_token(exp_in=-120))  # beyond the 30 s leeway


async def test_clock_skew_within_the_leeway_is_tolerated(validator, make_token) -> None:
    assert (await validator.tenant_id(make_token(exp_in=-10)))[0] == "support"


async def test_wrong_issuer_is_refused(validator, make_token) -> None:
    with pytest.raises(TokenError):
        await validator.tenant_id(make_token(iss="http://evil.example/realms/tollgate"))


async def test_wrong_audience_is_refused(validator, make_token) -> None:
    # A token Keycloak issued for some other app must not open the gateway.
    with pytest.raises(TokenError):
        await validator.tenant_id(make_token(aud="account"))


async def test_token_signed_with_an_unpublished_key_is_refused(
    validator, make_token, other_key
) -> None:
    with pytest.raises(TokenError):
        await validator.tenant_id(make_token(key=other_key))


async def test_tampered_payload_is_refused(validator, make_token) -> None:
    header, _, signature = make_token().split(".")
    forged_payload = jwt.utils.base64url_encode(b'{"tenant": "billing"}').decode()
    with pytest.raises(TokenError):
        await validator.tenant_id(f"{header}.{forged_payload}.{signature}")


async def test_unsigned_token_is_refused(validator) -> None:
    unsigned = jwt.encode({"tenant": "support"}, key=None, algorithm="none")
    with pytest.raises(TokenError):
        await validator.tenant_id(unsigned)


async def test_hmac_token_is_refused(validator) -> None:
    # Algorithm confusion: only RS256 is accepted, whatever the token's header claims.
    forged = jwt.encode({"tenant": "support"}, "x" * 32, algorithm="HS256", headers={"kid": "k1"})
    with pytest.raises(TokenError):
        await validator.tenant_id(forged)


async def test_token_without_a_tenant_claim_is_refused(validator, make_token) -> None:
    with pytest.raises(TokenError):
        await validator.tenant_id(make_token(tenant=None))


async def test_garbage_is_refused(validator) -> None:
    with pytest.raises(TokenError):
        await validator.tenant_id("not-a-jwt")


async def test_keys_are_fetched_once_and_cached(validator, make_token, keycloak) -> None:
    for _ in range(5):
        await validator.tenant_id(make_token())

    assert keycloak.fetches == 1


async def test_rotated_key_is_picked_up(validator, make_token, keycloak, other_key) -> None:
    await validator.tenant_id(make_token())  # caches k1
    keycloak.published["k2"] = other_key  # Keycloak rotates its signing key

    assert (await validator.tenant_id(make_token(key=other_key, kid="k2")))[0] == "support"
    assert keycloak.fetches == 2


async def test_unknown_key_ids_cant_force_repeated_fetches(
    validator, make_token, keycloak, other_key
) -> None:
    await validator.tenant_id(make_token())
    for kid in ("x1", "x2", "x3"):
        with pytest.raises(TokenError):
            await validator.tenant_id(make_token(key=other_key, kid=kid))

    assert keycloak.fetches == 2  # one re-fetch for the first unknown kid, then rate-limited
