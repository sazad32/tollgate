"""Billing settings, read once from environment variables (prefix TOLLGATE_BILLING_).

.NET: one typed options object bound from configuration, like IOptions<BillingOptions>.
"""

import socket
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TOLLGATE_BILLING_")

    # postgresql+asyncpg://billing:<password>@postgres.tollgate.svc:5432/billing
    database_url: SecretStr | None = None
    # redis://:<password>@redis.tollgate.svc:6379/0 (the gateway's Redis, ADR 0015)
    redis_url: SecretStr | None = None

    stream: str = "tollgate:usage"
    group: str = "billing"
    # This consumer's name in the group. In Kubernetes the host name is the pod name.
    consumer: str = Field(default_factory=socket.gethostname)
    batch: int = 100  # events per read and per transaction
    block_ms: int = 5000  # how long one read waits for new events
    claim_idle_s: float = 60.0  # pending this long on another consumer: its pod died, take over
    retry_s: float = 5.0  # wait between tries while Postgres or Redis is down
    retention_s: float = 86_400.0  # keep billed events in the stream this long, for replays
    maintain_every_s: float = 60.0  # how often to trim and forget dead consumers
    stale_consumer_s: float = 3600.0  # a consumer idle this long, owing nothing, is forgotten
    # Touched on every pass of the loop; the liveness probe checks its age. Unset: none.
    heartbeat_file: Path | None = None

    def require_database_url(self) -> str:
        if self.database_url is None:
            raise SystemExit("TOLLGATE_BILLING_DATABASE_URL is not set")
        return self.database_url.get_secret_value()

    def require_redis_url(self) -> str:
        if self.redis_url is None:
            raise SystemExit("TOLLGATE_BILLING_REDIS_URL is not set")
        return self.redis_url.get_secret_value()
