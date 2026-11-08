"""Billing settings, read once from environment variables (prefix TOLLGATE_BILLING_).

.NET: one typed options object bound from configuration, like IOptions<BillingOptions>.
"""

import socket
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from tollgate_billing.prices import PriceFileError, PriceList, load_prices
from tollgate_billing.reconcile import ReconcileBackend, Tolerance


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
    # The price list for the invoice commands (ADR 0017; deploy/billing/prices.toml).
    prices_file: Path | None = None
    # Reconciliation (ADR 0018): where Prometheus is, and the local backends to check, as JSON:
    # [{"backend": "llama-server-0", "kind": "llama"}, {"backend": "vllm", "kind": "vllm",
    #   "series": "vllm-workstation"}]. `series` is the Prometheus label, if it differs.
    prometheus_url: str | None = None
    reconcile_backends: list[ReconcileBackend] = []
    # How far a server's count may be from the ledger: by default only estimated tokens, by half.
    reconcile_tolerance_tokens: int = 0
    reconcile_tolerance_ratio: float = 0.0
    reconcile_estimate_share: float = 0.5

    def require_database_url(self) -> str:
        if self.database_url is None:
            raise SystemExit("TOLLGATE_BILLING_DATABASE_URL is not set")
        return self.database_url.get_secret_value()

    def require_redis_url(self) -> str:
        if self.redis_url is None:
            raise SystemExit("TOLLGATE_BILLING_REDIS_URL is not set")
        return self.redis_url.get_secret_value()

    def require_prices(self) -> PriceList:
        if self.prices_file is None:
            raise SystemExit("TOLLGATE_BILLING_PRICES_FILE is not set")
        try:
            return load_prices(self.prices_file)
        except (OSError, PriceFileError) as e:
            raise SystemExit(f"{self.prices_file}: {e}") from None

    def require_prometheus_url(self) -> str:
        if self.prometheus_url is None:
            raise SystemExit("TOLLGATE_BILLING_PROMETHEUS_URL is not set")
        return self.prometheus_url

    @property
    def tolerance(self) -> Tolerance:
        return Tolerance(self.reconcile_tolerance_tokens, self.reconcile_tolerance_ratio,
                         self.reconcile_estimate_share)  # fmt: skip
