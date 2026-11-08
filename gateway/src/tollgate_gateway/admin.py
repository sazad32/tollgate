"""Admin commands for tenants, API keys and the schema. Run inside a gateway pod, where the database
URL and pepper are already in the environment:

    kubectl -n tollgate exec deploy/tollgate-gateway -- python -m tollgate_gateway.admin <command>

    migrate                                   apply database migrations
    tenant create <id> --name "..." [--data-policy local_only|hosted_allowed] [--tier standard]
    tenant list | tenant disable <id> | tenant enable <id>
    key create <tenant> --name "..."          prints the new key ONCE; only its hash is stored
    key list [<tenant>] | key revoke <key id>
"""

import argparse
import asyncio
import re
import sys
from datetime import UTC, datetime
from importlib import resources

from alembic import command
from alembic.config import Config
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate_gateway.config import Settings
from tollgate_gateway.db import ApiKeyRow, TenantRow, make_engine
from tollgate_gateway.keys import display_prefix, generate_key, hash_key
from tollgate_gateway.tenants import DATA_POLICIES

TENANT_ID = re.compile(r"^[a-z][a-z0-9-]{1,62}$")


def database_url(settings: Settings) -> str:
    if settings.database_url is None:
        raise SystemExit("TOLLGATE_DATABASE_URL is not set")
    return settings.database_url.get_secret_value()


def migrate(settings: Settings) -> None:
    config = Config()
    config.set_main_option(
        "script_location", str(resources.files("tollgate_gateway") / "migrations")
    )
    config.attributes["database_url"] = database_url(settings)
    command.upgrade(config, "head")
    print("schema is up to date")


async def run(args: argparse.Namespace, settings: Settings) -> None:
    engine = make_engine(database_url(settings))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            await dispatch(args, session, settings)
    finally:
        await engine.dispose()


async def dispatch(args: argparse.Namespace, session: AsyncSession, settings: Settings) -> None:
    if args.area == "tenant":
        if args.action == "create":
            if not TENANT_ID.match(args.id):
                raise SystemExit("tenant id: lowercase letters, digits and dashes, e.g. 'support'")
            if args.data_policy not in DATA_POLICIES:
                raise SystemExit(f"--data-policy must be one of {DATA_POLICIES}")
            session.add(
                TenantRow(id=args.id, name=args.name, data_policy=args.data_policy, tier=args.tier)
            )
            await session.commit()
            print(f"created tenant {args.id}")
        elif args.action == "list":
            rows = (await session.scalars(select(TenantRow).order_by(TenantRow.id))).all()
            print(f"{'ID':<16} {'NAME':<28} {'DATA POLICY':<15} {'TIER':<10} DISABLED")
            for t in rows:
                print(f"{t.id:<16} {t.name:<28} {t.data_policy:<15} {t.tier:<10} {t.disabled}")
        else:  # disable / enable
            result = await session.execute(
                update(TenantRow)
                .where(TenantRow.id == args.id)
                .values(disabled=args.action == "disable")
            )
            await session.commit()
            if result.rowcount == 0:
                raise SystemExit(f"no tenant {args.id!r}")
            print(f"tenant {args.id} {args.action}d (takes effect within the auth cache time)")
    elif args.area == "key":
        if args.action == "create":
            if await session.get(TenantRow, args.tenant) is None:
                raise SystemExit(f"no tenant {args.tenant!r}")
            key = generate_key()
            pepper = settings.api_key_pepper.get_secret_value().encode()
            row = ApiKeyRow(
                tenant_id=args.tenant,
                name=args.name,
                prefix=display_prefix(key),
                key_hash=hash_key(key, pepper),
            )
            session.add(row)
            await session.commit()
            print(f"created key {row.id} for tenant {args.tenant}")
            print("Save this key now; it can't be shown again:")
            print(key)
        elif args.action == "list":
            query = select(ApiKeyRow).order_by(ApiKeyRow.tenant_id, ApiKeyRow.created_at)
            if args.tenant:
                query = query.where(ApiKeyRow.tenant_id == args.tenant)
            rows = (await session.scalars(query)).all()
            line = "{:<34} {:<12} {:<13} {:<20} {:<17} {}"
            print(line.format("KEY ID", "TENANT", "PREFIX", "NAME", "LAST USED", "REVOKED"))
            for k in rows:
                used = k.last_used_at.strftime("%Y-%m-%d %H:%M") if k.last_used_at else "never"
                revoked = k.revoked_at.strftime("%Y-%m-%d %H:%M") if k.revoked_at else ""
                print(line.format(k.id, k.tenant_id, k.prefix + "…", k.name, used, revoked))
        else:  # revoke
            result = await session.execute(
                update(ApiKeyRow)
                .where(ApiKeyRow.id == args.key_id, ApiKeyRow.revoked_at.is_(None))
                .values(revoked_at=datetime.now(UTC))
            )
            await session.commit()
            if result.rowcount == 0:
                raise SystemExit(f"no active key {args.key_id!r}")
            print(f"revoked key {args.key_id} (takes effect within the auth cache time)")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tollgate_gateway.admin", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    areas = p.add_subparsers(dest="area", required=True)
    areas.add_parser("migrate", help="apply database migrations")

    tenant = areas.add_parser("tenant").add_subparsers(dest="action", required=True)
    create = tenant.add_parser("create")
    create.add_argument("id")
    create.add_argument("--name", required=True)
    create.add_argument("--data-policy", default="local_only")
    create.add_argument("--tier", default="standard")
    tenant.add_parser("list")
    for action in ("disable", "enable"):
        tenant.add_parser(action).add_argument("id")

    key = areas.add_parser("key").add_subparsers(dest="action", required=True)
    key_create = key.add_parser("create")
    key_create.add_argument("tenant")
    key_create.add_argument("--name", required=True, help="what the key is for, e.g. 'support bot'")
    key.add_parser("list").add_argument("tenant", nargs="?")
    key.add_parser("revoke").add_argument("key_id")
    return p


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    settings = Settings()  # type: ignore[call-arg]  (environment supplies the values)
    if args.area == "migrate":
        migrate(settings)  # synchronous: Alembic's env.py runs its own event loop
    else:
        asyncio.run(run(args, settings))


if __name__ == "__main__":
    sys.exit(main())
