"""Tenants and API keys in Postgres (ADR 0010, ADR 0011).

.NET: SQLAlchemy 2.0 async is EF Core: the classes below are entities, an AsyncSession is a
DbContext, Alembic (migrations/) is EF migrations.
Gotcha: no change tracking across requests and no lazy loading in async code; each query says
exactly what it loads.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, false, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from tollgate_gateway.tenants import ApiKeyRecord, Tenant


class Base(DeclarativeBase):
    pass


class TenantRow(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    data_policy: Mapped[str] = mapped_column(String(32), server_default="local_only")
    tier: Mapped[str] = mapped_column(String(32), server_default="standard")
    disabled: Mapped[bool] = mapped_column(Boolean, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def to_tenant(self) -> Tenant:
        return Tenant(
            id=self.id,
            name=self.name,
            data_policy=self.data_policy,
            tier=self.tier,
            disabled=self.disabled,
        )


class ApiKeyRow(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    prefix: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def to_record(self) -> ApiKeyRecord:
        return ApiKeyRecord(
            id=self.id,
            tenant_id=self.tenant_id,
            prefix=self.prefix,
            key_hash=self.key_hash,
            revoked_at=self.revoked_at,
        )


def make_engine(database_url: str) -> AsyncEngine:
    # pool_pre_ping: check a pooled connection before use, so a Postgres restart costs one retry
    # instead of a failed request.
    return create_async_engine(database_url, pool_size=5, max_overflow=5, pool_pre_ping=True)


class PostgresAuthStore:
    """The production AuthStore. Matches the Protocol in tenants.py by shape, not by declaration."""

    def __init__(self, engine: AsyncEngine):
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def tenant(self, tenant_id: str) -> Tenant | None:
        async with self.sessions() as session:
            row = await session.get(TenantRow, tenant_id)
            return row.to_tenant() if row else None

    async def key_by_hash(self, key_hash: str) -> tuple[ApiKeyRecord, Tenant] | None:
        async with self.sessions() as session:
            result = await session.execute(
                select(ApiKeyRow, TenantRow)
                .join(TenantRow, ApiKeyRow.tenant_id == TenantRow.id)
                .where(ApiKeyRow.key_hash == key_hash)
            )
            found = result.first()
            if found is None:
                return None
            key, tenant = found
            # Recorded on cache misses only (about once a minute per key), not on every request.
            await session.execute(
                update(ApiKeyRow).where(ApiKeyRow.id == key.id).values(last_used_at=func.now())
            )
            await session.commit()
            return key.to_record(), tenant.to_tenant()
