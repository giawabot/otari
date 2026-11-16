"""Replace payment tables and webhook records with grant idempotency records."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_payments_leave_the_overlay"
down_revision: str | None = "0004_billing_fx_disputes_balance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("nr_e_transfers")
    op.drop_table("nr_subscriptions")
    op.drop_index("uq_nr_orgs_stripe_customer_id", table_name="nr_orgs")
    with op.batch_alter_table("nr_orgs") as batch_op:
        batch_op.drop_column("stripe_customer_id")

    op.rename_table("nr_webhook_events", "nr_grant_events")
    op.execute("DELETE FROM nr_grant_events")
    with op.batch_alter_table("nr_grant_events") as batch_op:
        batch_op.alter_column(
            "event_id",
            new_column_name="idempotency_key",
            existing_type=sa.String(length=255),
            nullable=False,
        )
        batch_op.alter_column(
            "event_type",
            new_column_name="source_ref",
            existing_type=sa.String(length=255),
            nullable=True,
        )
        batch_op.alter_column(
            "processed_at",
            new_column_name="created_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )
        batch_op.drop_column("succeeded")
        batch_op.add_column(sa.Column("payload_fingerprint", sa.String(length=64), nullable=False))
        batch_op.add_column(sa.Column("org_id", sa.Uuid(), nullable=False))
        batch_op.add_column(sa.Column("amount", sa.Numeric(18, 6), nullable=False))
        batch_op.add_column(sa.Column("reason", sa.String(length=500), nullable=False))
        batch_op.add_column(sa.Column("ledger_entry_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("balance_after", sa.Numeric(18, 6), nullable=True))
        batch_op.create_foreign_key(
            "fk_nr_grant_events_org_id_nr_orgs",
            "nr_orgs",
            ["org_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    op.execute("UPDATE nr_grant_events SET source_ref = 'grant' WHERE source_ref IS NULL")
    with op.batch_alter_table("nr_grant_events") as batch_op:
        batch_op.drop_column("balance_after")
        batch_op.drop_column("ledger_entry_id")
        batch_op.drop_column("reason")
        batch_op.drop_column("amount")
        batch_op.drop_column("org_id")
        batch_op.drop_column("payload_fingerprint")
        batch_op.add_column(sa.Column("succeeded", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.alter_column(
            "created_at",
            new_column_name="processed_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )
        batch_op.alter_column(
            "source_ref",
            new_column_name="event_type",
            existing_type=sa.String(length=255),
            nullable=False,
        )
        batch_op.alter_column(
            "idempotency_key",
            new_column_name="event_id",
            existing_type=sa.String(length=255),
            nullable=False,
        )
    op.rename_table("nr_grant_events", "nr_webhook_events")

    op.add_column("nr_orgs", sa.Column("stripe_customer_id", sa.String(length=255), nullable=True))
    op.create_index("uq_nr_orgs_stripe_customer_id", "nr_orgs", ["stripe_customer_id"], unique=True)
    op.create_table(
        "nr_subscriptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("stripe_subscription_id", sa.String(length=255), nullable=False),
        sa.Column("price_id", sa.String(length=255), nullable=True),
        sa.Column("plan", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("credit_amount", sa.Numeric(18, 6), nullable=False),
        sa.Column("last_event_created", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["nr_orgs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stripe_subscription_id"),
    )
    op.create_index("ix_nr_subscriptions_org_id", "nr_subscriptions", ["org_id"])
    op.create_table(
        "nr_e_transfers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 6), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="CAD"),
        sa.Column("reference", sa.String(length=255), nullable=False),
        sa.Column("reference_normalized", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("fx_rate", sa.Numeric(18, 6), nullable=True),
        sa.Column("fx_rate_date", sa.String(length=32), nullable=True),
        sa.Column("fx_source", sa.String(length=64), nullable=True),
        sa.Column("fx_margin", sa.Numeric(18, 6), nullable=True),
        sa.Column("credited_usd", sa.Numeric(18, 6), nullable=True),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="ck_nr_e_transfers_status"),
        sa.ForeignKeyConstraint(["org_id"], ["nr_orgs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_nr_e_transfers_org_id", "nr_e_transfers", ["org_id"])
    op.create_index("uq_nr_e_transfers_reference_normalized", "nr_e_transfers", ["reference_normalized"], unique=True)
    with op.batch_alter_table("nr_e_transfers") as batch_op:
        batch_op.create_unique_constraint("uq_nr_e_transfers_org_reference", ["org_id", "reference"])
