"""Add organization.residency_floor (NorthRouter plan 01b).

One nullable bar-name column per organization: "canadian", "sovereign",
or "sovereign_model", or NULL for no floor. The request path folds it
into the effective residency bar with max(request_bar, org_floor),
resolved from the billed key's organization, so no key configuration
escapes it.

Revision ID: e3a6b9c1d5f8
Revises: d6a8c0e2f4b6
Create Date: 2026-09-17 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e3a6b9c1d5f8"
down_revision: str | Sequence[str] | None = "d6a8c0e2f4b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "organization",
        sa.Column("residency_floor", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("organization", "residency_floor")
