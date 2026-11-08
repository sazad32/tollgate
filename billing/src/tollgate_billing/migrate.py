"""Apply (or roll back) billing's schema with Alembic. Synchronous: Alembic's env.py runs its own
event loop, so call these from plain code, never from inside a running asyncio loop."""

from importlib import resources

from alembic import command
from alembic.config import Config


def alembic_config(database_url: str) -> Config:
    config = Config()
    config.set_main_option(
        "script_location", str(resources.files("tollgate_billing") / "migrations")
    )
    config.attributes["database_url"] = database_url
    return config


def upgrade(database_url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url), revision)


def downgrade(database_url: str, revision: str = "base") -> None:
    command.downgrade(alembic_config(database_url), revision)
