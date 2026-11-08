"""Gateway settings, read once from environment variables (prefix TOLLGATE_).

.NET: one typed options object bound from configuration, like IOptions<GatewayOptions>.
Gotcha: values are read when Settings() is created, not on every access; restart to pick up changes.
"""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from tollgate_gateway.limits import Limit


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TOLLGATE_")

    # --- Upstream model server (ADR 0009) ---------------------------------------------------------
    # The one model server this gateway forwards to. The week 8 router replaces this with several.
    upstream_url: str = "http://llama-server.tollgate.svc:8080"
    # Sent upstream as a bearer token when set (vLLM needs one; llama-server ignores it).
    upstream_api_key: SecretStr | None = None
    # A request queued on a busy llama-server can wait minutes for a first byte (bench/RESULTS.md).
    upstream_read_timeout_s: float = 900.0
    upstream_connect_timeout_s: float = 5.0
    # Below llama-server's 5 s keep-alive, so we never reuse a connection the server is closing
    # (the week 4 benchmark lost 6 requests to exactly that race).
    upstream_keepalive_expiry_s: float = 2.0
    upstream_max_connections: int = 200

    # --- Authentication (ADR 0010) ----------------------------------------------------------------
    # Secret key for hashing API keys. Required: the gateway refuses to start without it.
    api_key_pepper: SecretStr
    # Tenants and API keys: postgresql+asyncpg://user:password@host:5432/tollgate
    database_url: SecretStr | None = None
    # Keycloak realm that issues tokens. Unset: only API keys are accepted.
    oidc_issuer: str | None = None
    oidc_audience: str = "tollgate-gateway"
    # Where to fetch the realm's signing keys. Default: the issuer's standard JWKS path.
    oidc_jwks_url: str | None = None
    # How long a checked key or tenant is trusted before the database is asked again.
    auth_cache_ttl_s: float = 60.0

    # --- Rate limits (ADR 0012) ------------------------------------------------------------------
    # Redis for the token buckets: redis://:password@host:6379/0. Unset: no rate limits.
    redis_url: SecretStr | None = None
    # Tokens per minute (prompt + output) by tenant tier, as JSON in TOLLGATE_RATE_LIMITS.
    # A tier not listed here gets the "standard" limit.
    rate_limits: dict[str, int] = {"standard": 10_000}

    def limit_for(self, tier: str) -> Limit:
        tokens = self.rate_limits.get(tier, self.rate_limits.get("standard", 10_000))
        return Limit(tokens_per_minute=tokens)

    @property
    def jwks_url(self) -> str | None:
        if self.oidc_jwks_url:
            return self.oidc_jwks_url
        if self.oidc_issuer:
            return self.oidc_issuer.rstrip("/") + "/protocol/openid-connect/certs"
        return None
