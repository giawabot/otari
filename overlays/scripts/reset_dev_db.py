"""One-time reset for DEV databases that ran the pre-reset migration chain.

The overlay's Alembic chain was collapsed to a single ``nr_0001_initial``
before any deployment, so dev databases holding the old 0001-0005 history
cannot upgrade forward. This script drops every ``nr_*`` table (including
``nr_alembic_version``) and rebuilds the schema from ``nr_0001_initial``.

DEV DATABASES ONLY. Otari's own tables are never touched: the drop is
restricted to names starting with ``nr_``, the same ownership rule the
migration chain enforces via ``migration_filter.TABLE_PREFIX``.

Usage:
    NORTHROUTER_DATABASE_URL=sqlite:////path/to/dev.db \
        uv run python overlays/scripts/reset_dev_db.py --yes
"""

import argparse
import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, create_engine, inspect, text

INI_PATH = Path(__file__).resolve().parent.parent / "northrouter_billing" / "alembic.ini"
TABLE_PREFIX = "nr_"


def _sync_url(raw: str) -> str:
    """Accept the async driver spellings the overlay's env normally carries."""
    for async_driver, sync_driver in (("sqlite+aiosqlite", "sqlite"), ("postgresql+asyncpg", "postgresql")):
        if raw.startswith(f"{async_driver}://"):
            return f"{sync_driver}://{raw.removeprefix(f'{async_driver}://')}"
    return raw


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset a dev database to the collapsed overlay migration chain.")
    parser.add_argument("--yes", action="store_true", help="actually drop the tables (default: dry run)")
    args = parser.parse_args()

    raw_url = os.getenv("NORTHROUTER_DATABASE_URL", "").strip()
    if not raw_url:
        print("NORTHROUTER_DATABASE_URL must be set to the dev database", file=sys.stderr)
        return 2
    engine = create_engine(_sync_url(raw_url))

    names = [name for name in inspect(engine).get_table_names() if name.startswith(TABLE_PREFIX)]
    if not names:
        print("No nr_* tables present; nothing to drop.")
    elif not args.yes:
        print(f"Dry run: would drop {len(names)} tables: {', '.join(sorted(names))}\nRe-run with --yes.")
    else:
        metadata = MetaData()
        metadata.reflect(bind=engine, only=names)
        with engine.begin() as conn:
            for table in reversed(metadata.sorted_tables):
                conn.execute(text(f'DROP TABLE IF EXISTS "{table.name}"'))
        print(f"Dropped {len(names)} nr_* tables.")

    command.upgrade(Config(str(INI_PATH)), "head")
    print("Schema rebuilt at head (nr_0001_initial).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
