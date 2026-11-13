"""Gateway settings, read once from environment variables (prefix TOLLGATE_).

.NET: one typed options object bound from configuration, like IOptions<GatewayOptions>.
Gotcha: values are read when Settings() is created, not on every access; restart to pick up changes.
"""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from tollgate_gateway.backends import Backend
from tollgate_gateway.limits import Limit


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TOLLGATE_")

    # --- Local model servers (ADR 0009, 0013) -----------------------------------------------------
    # The backends the router chooses from, as JSON in TOLLGATE_BACKENDS. The default is the
    # llama-server Service, for running the gateway outside the cluster's config.
    backends: list[Backend] = [
        Backend(name="llama-server", url="http://llama-server.tollgate.svc:8080", slots=4)
    ]
    # The model every local backend serves (ADR 0005). Any model name other than hosted_model goes
    # to a local backend.
    local_model: str = "smollm3-3b"
    # How often to read each backend's /metrics. 0 turns polling off (tests poll by hand).
    load_poll_interval_s: float = 1.0
    # Sent to local backends as a bearer token when set (vLLM needs one; llama-server ignores it).
    upstream_api_key: SecretStr | None = None
    # A request queued on a busy llama-server can wait minutes for a first byte (bench/RESULTS.md).
    upstream_read_timeout_s: float = 900.0
    upstream_connect_timeout_s: float = 5.0
    # Below llama-server's 5 s keep-alive, so we never reuse a connection the server is closing
    # (the week 4 benchmark lost 6 requests to exactly that race).
    upstream_keepalive_expiry_s: float = 2.0
    # Connections to the model servers, at most. Unset: twice the backends' slots, at least 200
    # (more requests than slots wait in the fair queue, not here).
    upstream_max_connections: int | None = Field(default=None, ge=1)
    # Split across this many separate pools (ADR 0020): httpcore's bookkeeping grows with the square
    # of one pool's size, so several small pools cost far less CPU than one big one.
    upstream_pools: int = Field(default=16, ge=1)

    # --- Hosted model (ADR 0013) ------------------------------------------------------------------
    # Unset: no hosted model at all; every request stays local.
    anthropic_api_key: SecretStr | None = None
    # Clients ask for it by this name, and hosted_allowed tenants overflow to it.
    hosted_model: str = "claude-opus-5-5"
    # How hard Claude thinks before answering (low, medium, high...). Empty: the model's default.
    hosted_effort: str | None = "low"
    # Claude needs a max_tokens; used when the client sent none. Thinking counts towards it.
    hosted_max_tokens: int = 4096
    # Anthropic's server-side refusal fallback: a declined request is retried on another model.
    hosted_refusal_fallback: bool = True
    hosted_timeout_s: float = 600.0
    hosted_max_retries: int = 2  # the SDK's retries, before the first byte only

    # --- Fair queuing (ADR 0014) ------------------------------------------------------------------
    # Hold requests in the gateway when every local slot is busy, and hand out free slots fairly
    # between tenants. Needs Redis; off (false) means requests queue inside the backends instead.
    fair_queue: bool = True
    queue_max_waiting: int = 16  # per tenant; one more is a 429
    queue_max_wait_s: float = 120.0  # then a 429
    queue_lease_s: float = 30.0  # a dead replica's slots come back after this long
    queue_poll_s: float = 0.1  # how often a waiting request checks whether it's its turn

    # --- Load (ADR 0020) --------------------------------------------------------------------------
    # Model requests one replica serves at once; past it, a quick 503 + Retry-After instead of
    # piling up until the pod runs out of memory. Unset: no cap.
    max_in_flight: int | None = Field(default=None, ge=0)
    # Redis connections, at most. Past it a call waits up to redis_pool_timeout_s for a free one,
    # then fails (and that step fails open). redis-py's default pool has 100 and doesn't wait.
    redis_max_connections: int = Field(default=500, ge=1)
    redis_pool_timeout_s: float = 0.5

    # --- Metrics and the event loop (ADR 0019) ---------------------------------------------------
    # How often to measure event-loop lag; 0 turns it off.
    loop_lag_interval_s: float = 0.25
    # asyncio's debug mode: logs every step that holds the event loop longer than slow_callback_s,
    # naming the code. It slows everything down, so only for a load-test run.
    loop_debug: bool = False
    slow_callback_s: float = 0.1

    # --- Usage events (ADR 0015) ------------------------------------------------------------------
    # One event per request a model ran, written to this Redis stream for billing. No Redis: none.
    usage_stream: str = "tollgate:usage"
    usage_buffer_max: int = 10_000  # events kept in memory per replica while Redis is down
    usage_retry_s: float = 5.0  # how often to retry Redis while it's down

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

    @property
    def max_upstream_connections(self) -> int:
        if self.upstream_max_connections is not None:
            return self.upstream_max_connections
        return max(200, 2 * sum(b.slots for b in self.backends))

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
