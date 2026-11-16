"""Which database objects the overlay's Alembic chain owns."""

from typing import Any

VERSION_TABLE = "nr_alembic_version"
TABLE_PREFIX = "nr_"


def include_object(obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any) -> bool:
    """Keep autogenerate to this overlay's tables, so it never proposes dropping Otari's."""
    del obj, reflected, compare_to
    if type_ == "table":
        return name is not None and name.startswith(TABLE_PREFIX) and name != VERSION_TABLE
    return True
