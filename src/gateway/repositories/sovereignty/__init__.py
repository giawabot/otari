"""Data access for provider sovereignty attestations."""

from gateway.repositories.sovereignty.sovereignty_repository import (
    list_sovereignty_rows,
    upsert_sovereignty,
)

__all__ = ["list_sovereignty_rows", "upsert_sovereignty"]
