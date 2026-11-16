"""Record which registered OAuth app an API key was issued to.

Adds ``oauth_client_id`` to ``api_keys``, written only when an app's
authorization grant is exchanged for a key. The copy in ``metadata`` is
caller-editable and so proves nothing; this column is not. Existing app keys
are not backfilled from ``metadata``, since a member could have written that
value themselves; they gain the column the next time the app signs in.

Revision ID: f9c2e4a6b8d1
Revises: e3a6b9c1d5f8
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f9c2e4a6b8d1"
down_revision: str | Sequence[str] | None = "e3a6b9c1d5f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the column; no existing key has a recorded app."""
    op.add_column("api_keys", sa.Column("oauth_client_id", sa.String(length=128), nullable=True))


def downgrade() -> None:
    """Drop the column."""
    with op.batch_alter_table("api_keys") as batch:
        batch.drop_column("oauth_client_id")
