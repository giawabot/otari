"""Add provider_sovereignty table and usage_logs.residency_audit column.

NorthRouter residency routing (plan 01): the attestation table records
each provider instance's sovereignty level and supporting facts; the
usage column carries proof-of-routing (requested bar, served level) on
the row that settled a residency-gated request.

Revision ID: d6a8c0e2f4b6
Revises: c4e7a9b2d6f8
Create Date: 2026-09-14 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d6a8c0e2f4b6"
down_revision: str | Sequence[str] | None = "c4e7a9b2d6f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "provider_sovereignty",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=True),
        sa.Column("provider", sa.String(length=255), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("sovereignty_level", sa.Integer(), nullable=False),
        sa.Column("hosted_in", sa.String(length=8), nullable=False),
        sa.Column("operator_jurisdiction", sa.String(length=64), nullable=False),
        sa.Column("model_origin", sa.String(length=64), nullable=True),
        sa.Column("vetted", sa.Boolean(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "provider", "model", name="uq_provider_sovereignty_org_provider_model"),
    )
    op.create_index("ix_provider_sovereignty_provider", "provider_sovereignty", ["provider"])
    op.create_index("ix_provider_sovereignty_organization", "provider_sovereignty", ["organization_id"])
    op.create_index(
        op.f("ix_provider_sovereignty_id"),
        "provider_sovereignty",
        ["id"],
        unique=False,
    )
    op.add_column("usage_logs", sa.Column("residency_audit", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("usage_logs", "residency_audit")
    op.drop_index("ix_provider_sovereignty_id", table_name="provider_sovereignty")
    op.drop_index("ix_provider_sovereignty_organization", table_name="provider_sovereignty")
    op.drop_index("ix_provider_sovereignty_provider", table_name="provider_sovereignty")
    op.drop_table("provider_sovereignty")
