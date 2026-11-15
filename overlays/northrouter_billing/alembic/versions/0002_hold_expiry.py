"""Add expiration timestamps to NorthRouter billing holds."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_hold_expiry"
down_revision: Union[str, None] = "0001_northrouter_billing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("nr_holds", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("nr_holds", "expires_at")
