"""Validate Keycloak access tokens (ADR 0003, ADR 0010).

.NET: what AddJwtBearer does with an Authority: fetch the issuer's signing keys (JWKS), then check
signature, expiry, issuer and audience. Here it's explicit, and the tenant comes from a claim.
"""

import time

import httpx
import jwt

# Only Keycloak's default signing algorithm. Never taken from the token: a token that says "none" or
# "HS256" (signed with our public key as an HMAC secret) must not get to choose how it's checked.
ALGORITHMS = ["RS256"]


class TokenError(Exception):
    """The token is not acceptable. The message is safe to log, not to return to the caller."""


class KeysUnavailableError(Exception):
    """Keycloak's signing keys couldn't be fetched: our outage, not the caller's mistake."""


class JwksCache:
    """Keycloak's public signing keys, fetched over HTTP and cached by key ID (`kid`).

    An unknown kid triggers one re-fetch (Keycloak rotated its keys), but no more than once per
    `min_refresh_s`, so a flood of forged tokens can't turn into a flood of requests to Keycloak.
    """

    RETRY_INITIAL_S = 5.0  # while Keycloak has never answered, try again at most this often

    def __init__(self, client: httpx.AsyncClient, jwks_url: str, min_refresh_s: float = 30.0):
        self.client = client
        self.jwks_url = jwks_url
        self.min_refresh_s = min_refresh_s
        self.fetches = 0
        self._keys: dict[str, jwt.PyJWK] = {}
        self._loaded = False
        self._last_attempt = float("-inf")
        self._last_refresh = float("-inf")

    async def key(self, kid: str) -> jwt.PyJWK:
        if kid not in self._keys:
            now = time.monotonic()
            if not self._loaded:
                if now - self._last_attempt >= self.RETRY_INITIAL_S:
                    await self._fetch(now)
            elif now - self._last_refresh >= self.min_refresh_s:
                self._last_refresh = now
                await self._fetch(now)
        try:
            return self._keys[kid]
        except KeyError:
            raise TokenError(f"unknown signing key {kid!r}") from None

    async def _fetch(self, now: float) -> None:
        self._last_attempt = now
        self.fetches += 1
        try:
            response = await self.client.get(self.jwks_url)
            response.raise_for_status()
            document = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise KeysUnavailableError(f"could not fetch signing keys: {exc!r}") from exc
        keys = {}
        for entry in document.get("keys", []):
            if entry.get("use", "sig") == "sig" and "kid" in entry:
                try:
                    keys[entry["kid"]] = jwt.PyJWK(entry)
                except jwt.PyJWTError:
                    continue  # a key type we don't use, e.g. Keycloak's encryption key
        self._keys = keys
        self._loaded = True


class JwtValidator:
    def __init__(
        self,
        jwks: JwksCache,
        issuer: str,
        audience: str,
        tenant_claim: str = "tenant",
        leeway_s: int = 30,
    ):
        self.jwks = jwks
        self.issuer = issuer
        self.audience = audience
        self.tenant_claim = tenant_claim
        self.leeway_s = leeway_s

    async def tenant_id(self, token: str) -> tuple[str, str]:
        """Validate `token`; return (tenant id, subject). Raise TokenError if it's refused."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise TokenError(f"malformed token: {exc}") from exc
        if header.get("alg") not in ALGORITHMS or not header.get("kid"):
            raise TokenError(f"unexpected algorithm {header.get('alg')!r} or missing key ID")
        signing_key = await self.jwks.key(header["kid"])
        try:
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=ALGORITHMS,
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway_s,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise TokenError(f"invalid token: {exc}") from exc
        tenant = claims.get(self.tenant_claim)
        if not isinstance(tenant, str) or not tenant:
            raise TokenError(f"token has no {self.tenant_claim!r} claim")
        return tenant, claims["sub"]
