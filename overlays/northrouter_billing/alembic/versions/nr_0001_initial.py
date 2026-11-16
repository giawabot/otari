"""Create the NorthRouter billing tables.

Revision ID: nr_0001_initial
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "nr_0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MONEY = sa.Numeric(18, 6)


def upgrade() -> None:
    op.create_table(
        "nr_orgs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("otari_org_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("suspend_reason", sa.String(length=255), nullable=True),
        sa.Column("status_effective_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("balance", _MONEY, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active', 'suspended')", name="ck_nr_orgs_status"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_nr_orgs_otari_org_id", "nr_orgs", ["otari_org_id"], unique=True)

    op.create_table(
        "nr_ledger_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("amount", _MONEY, nullable=False),
        sa.Column("ref", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("api_key_id", sa.String(length=255), nullable=True),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("metadata_json", sa.String(), nullable=True),
        sa.CheckConstraint(
            "type IN ('grant', 'consume', 'hold', 'release', 'refund')",
            name="ck_nr_ledger_type",
        ),
        sa.ForeignKeyConstraint(["org_id"], ["nr_orgs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("type", "ref", name="uq_nr_ledger_type_ref"),
    )
    op.create_index("ix_nr_ledger_org_created", "nr_ledger_entries", ["org_id", "created_at", "id"])

    op.create_table(
        "nr_holds",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("amount", _MONEY, nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ref", sa.String(length=255), nullable=True),
        sa.CheckConstraint("state IN ('open', 'released')", name="ck_nr_holds_state"),
        sa.ForeignKeyConstraint(["org_id"], ["nr_orgs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_nr_holds_open", "nr_holds", ["org_id", "state", "amount"])
    op.create_index("ix_nr_holds_state_expires", "nr_holds", ["state", "expires_at"])

    op.create_table(
        "nr_grant_events",
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("payload_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("amount", _MONEY, nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("principal", sa.String(length=32), nullable=False),
        sa.Column("ledger_entry_id", sa.Uuid(), nullable=True),
        sa.Column("balance_after", _MONEY, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["nr_orgs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("idempotency_key"),
    )

    op.create_table(
        "nr_status_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("from_status", sa.String(length=16), nullable=False),
        sa.Column("to_status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied", sa.Boolean(), nullable=False),
        sa.Column("principal", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["nr_orgs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_nr_status_events_org_created", "nr_status_events", ["org_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_nr_status_events_org_created", table_name="nr_status_events")
    op.drop_table("nr_status_events")
    op.drop_table("nr_grant_events")
    op.drop_index("ix_nr_holds_state_expires", table_name="nr_holds")
    op.drop_index("ix_nr_holds_open", table_name="nr_holds")
    op.drop_table("nr_holds")
    op.drop_index("ix_nr_ledger_org_created", table_name="nr_ledger_entries")
    op.drop_table("nr_ledger_entries")
    op.drop_index("ix_nr_orgs_otari_org_id", table_name="nr_orgs")
    op.drop_table("nr_orgs")
