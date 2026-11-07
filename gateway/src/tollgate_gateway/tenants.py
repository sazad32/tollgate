"""Tenants and API-key records, and the store the gateway reads them from (ADR 0010).

.NET: AuthStore is an interface (Protocol) with two implementations, like IAuthStore with a Postgres
one for production (db.py) and an in-memory one for tests.
Gotcha: a Protocol is structural. A class matches it by having the right methods; it never declares
`: AuthStore`.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class Tenant:
    """A team that calls the API and gets billed: a support, billing or engineering team."""

    id: str  # short slug, e.g. "support"; also the `tenant` claim in Keycloak tokens
    name: str
    data_policy: str = "local_only"  # may requests leave the network? (week 8 router)
    tier: str = "standard"  # price and limit tier (weeks 7 and 12)
    disabled: bool = False


@dataclass(frozen=True)
class ApiKeyRecord:
    """What's stored for a key. Never the key itself: only its keyed hash (keys.py)."""

    id: str
    tenant_id: str
    prefix: str  # first characters, safe to show in listings
    key_hash: str
    revoked_at: datetime | None = None


class AuthStore(Protocol):
    async def tenant(self, tenant_id: str) -> Tenant | None: ...

    async def key_by_hash(self, key_hash: str) -> tuple[ApiKeyRecord, Tenant] | None: ...


@dataclass
class InMemoryAuthStore:
    """AuthStore for tests: plain dicts, plus a switch to simulate the database being down."""

    tenants: dict[str, Tenant] = field(default_factory=dict)
    keys: dict[str, ApiKeyRecord] = field(default_factory=dict)  # by key_hash
    fail: bool = False
    calls: int = 0

    async def tenant(self, tenant_id: str) -> Tenant | None:
        self._touch()
        return self.tenants.get(tenant_id)

    async def key_by_hash(self, key_hash: str) -> tuple[ApiKeyRecord, Tenant] | None:
        self._touch()
        record = self.keys.get(key_hash)
        if record is None:
            return None
        return record, self.tenants[record.tenant_id]

    def _touch(self) -> None:
        self.calls += 1
        if self.fail:
            raise ConnectionError("database unavailable")
