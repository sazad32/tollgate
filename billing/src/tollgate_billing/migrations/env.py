"""Alembic runtime for billing's schema (ADR 0016). Run through
`python -m tollgate_billing migrate`, which passes the database URL in `config.attributes` rather
than in alembic.ini, so a password containing `%` can't upset the config parser.

.NET: like `dotnet ef database update`, with the migration classes in versions/.
"""

import asyncio

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from tollgate_billing.db import Base

config = context.config
target_metadata = Base.metadata


def run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    engine = create_async_engine(config.attributes["database_url"])
    async with engine.connect() as connection:
        await connection.run_sync(run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("offline (SQL script) migrations aren't set up; run against the database")
asyncio.run(run_online())
