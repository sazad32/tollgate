"""Tenants and API-key records, and the store the gateway reads them from (ADR 0010).

.NET: AuthStore is an interface (Protocol) with two implementations, like IAuthStore with a Postgres
one for production (db.py) and an in-memory one for tests.
Gotcha: a Protocol is structural. A class matches it by having the right methods; it never declares
`: AuthStore`.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

# May a tenant's requests leave the network? (ADR 0013)
LOCAL_ONLY = "local_only"  # never: local model servers only (the default)
HOSTED_ALLOWED = "hosted_allowed"  # yes: the hosted model, when asked for or when local is full
DATA_POLICIES = (LOCAL_ONLY, HOSTED_ALLOWED)


@dataclass(frozen=True)
class Tenant:
    """A team that calls the API and gets billed: a support, billing or engineering team."""

    id: str  # short slug, e.g. "support"; also the `tenant` claim in Keycloak tokens
    name: str
    data_policy: str = LOCAL_ONLY
    tier: str = "standard"  # price and limit tier (weeks 7 and 12)
    disabled: bool = False

    @property
    def allows_hosted(self) -> bool:
        """Only the exact value "hosted_allowed" lets a request leave the network. Anything else,
        including a typo in the database, keeps it local: fail closed."""
        return self.data_policy == HOSTED_ALLOWED


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

    async def mark_used(self, key_id: str) -> None: ...


@dataclass
class InMemoryAuthStore:
    """AuthStore for tests: plain dicts, plus a switch to simulate the database being down."""

    tenants: dict[str, Tenant] = field(default_factory=dict)
    keys: dict[str, ApiKeyRecord] = field(default_factory=dict)  # by key_hash
    fail: bool = False
    calls: int = 0
    used: list[str] = field(default_factory=list)  # key ids, in the order they were marked

    async def tenant(self, tenant_id: str) -> Tenant | None:
        self._touch()
        return self.tenants.get(tenant_id)

    async def key_by_hash(self, key_hash: str) -> tuple[ApiKeyRecord, Tenant] | None:
        self._touch()
        record = self.keys.get(key_hash)
        if record is None:
            return None
        return record, self.tenants[record.tenant_id]

    async def mark_used(self, key_id: str) -> None:
        self.used.append(key_id)

    def _touch(self) -> None:
        self.calls += 1
        if self.fail:
            raise ConnectionError("database unavailable")
