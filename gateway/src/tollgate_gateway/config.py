"""Gateway settings, read once from environment variables (prefix TOLLGATE_).

.NET: one typed options object bound from configuration, like IOptions<GatewayOptions>.
Gotcha: values are read when Settings() is created, not on every access; restart to pick up changes.
"""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TOLLGATE_")

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
