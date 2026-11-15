"""Create NorthRouter billing tables."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_northrouter_billing"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "nr_orgs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("otari_org_id", sa.Uuid(), nullable=False),
        sa.Column("stripe_customer_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active', 'suspended')", name="ck_nr_orgs_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("otari_org_id"),
    )
    op.create_index("ix_nr_orgs_otari_org_id", "nr_orgs", ["otari_org_id"], unique=True)
    op.create_table(
        "nr_ledger_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("amount", sa.Numeric(18, 6), nullable=False),
        sa.Column("ref", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("api_key_id", sa.String(length=255), nullable=True),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("metadata_json", sa.String(), nullable=True),
        sa.CheckConstraint(
            "type IN ('grant', 'consume', 'hold', 'release', 'refund', 'invoice')",
            name="ck_nr_ledger_type",
        ),
        sa.ForeignKeyConstraint(["org_id"], ["nr_orgs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("type", "ref", name="uq_nr_ledger_type_ref"),
    )
    op.create_index("ix_nr_ledger_entries_org_id", "nr_ledger_entries", ["org_id"])
    op.create_index("ix_nr_ledger_org_created", "nr_ledger_entries", ["org_id", "created_at"])
    op.create_table(
        "nr_holds",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 6), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ref", sa.String(length=255), nullable=True),
        sa.CheckConstraint("state IN ('open', 'released')", name="ck_nr_holds_state"),
        sa.ForeignKeyConstraint(["org_id"], ["nr_orgs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_nr_holds_org_id", "nr_holds", ["org_id"])
    op.create_index("ix_nr_holds_open", "nr_holds", ["org_id", "state", "amount"])
    op.create_table(
        "nr_e_transfers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 6), nullable=False),
        sa.Column("reference", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="ck_nr_e_transfers_status"),
        sa.ForeignKeyConstraint(["org_id"], ["nr_orgs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_nr_e_transfers_org_id", "nr_e_transfers", ["org_id"])
    op.create_table(
        "nr_subscriptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("stripe_subscription_id", sa.String(length=255), nullable=False),
        sa.Column("plan", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("credit_amount", sa.Numeric(18, 6), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["nr_orgs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stripe_subscription_id"),
    )
    op.create_index("ix_nr_subscriptions_org_id", "nr_subscriptions", ["org_id"])
    op.create_table(
        "nr_webhook_events",
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=255), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("succeeded", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("event_id"),
    )


def downgrade() -> None:
    op.drop_table("nr_webhook_events")
    op.drop_index("ix_nr_subscriptions_org_id", table_name="nr_subscriptions")
    op.drop_table("nr_subscriptions")
    op.drop_index("ix_nr_e_transfers_org_id", table_name="nr_e_transfers")
    op.drop_table("nr_e_transfers")
    op.drop_index("ix_nr_holds_open", table_name="nr_holds")
    op.drop_index("ix_nr_holds_org_id", table_name="nr_holds")
    op.drop_table("nr_holds")
    op.drop_index("ix_nr_ledger_org_created", table_name="nr_ledger_entries")
    op.drop_index("ix_nr_ledger_entries_org_id", table_name="nr_ledger_entries")
    op.drop_table("nr_ledger_entries")
    op.drop_index("ix_nr_orgs_otari_org_id", table_name="nr_orgs")
    op.drop_table("nr_orgs")
