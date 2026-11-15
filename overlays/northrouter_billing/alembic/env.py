"""Alembic environment for the tables owned by the billing overlay."""

import os
from logging.config import fileConfig

from alembic import context
from northrouter_billing.models import BillingBase
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = BillingBase.metadata


def _database_url() -> str:
    """Resolve a deliberate target URL and translate async driver URLs for Alembic."""
    value = config.get_main_option("sqlalchemy.url") or os.getenv("NORTHROUTER_DATABASE_URL")
    if not value:
        value = os.getenv("OTARI_DATABASE_URL", "")
    if not value:
        raise RuntimeError("Set NORTHROUTER_DATABASE_URL or OTARI_DATABASE_URL before running Alembic")
    url = make_url(value)
    if url.drivername == "sqlite+aiosqlite":
        url = url.set(drivername="sqlite")
    elif url.drivername in {"postgresql+asyncpg", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg2")
    return url.render_as_string(hide_password=False)


def run_migrations_offline() -> None:
    """Emit the overlay's migration SQL without opening a connection."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table="nr_alembic_version",
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run this chain on the explicitly configured Otari database."""
    config.set_main_option("sqlalchemy.url", _database_url().replace("%", "%%"))
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table="nr_alembic_version",
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
