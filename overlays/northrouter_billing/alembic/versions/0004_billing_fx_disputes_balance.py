"""Add FX columns, global normalized e-transfer reference, dispute ledger type, and running balance."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_billing_fx_disputes_balance"
down_revision: str | None = "0003_billing_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "nr_orgs",
        sa.Column("balance", sa.Numeric(18, 6), nullable=False, server_default="0"),
    )
    op.execute(
        """
        UPDATE nr_orgs
        SET balance = COALESCE(
            (
                SELECT SUM(
                    CASE
                        WHEN type IN ('grant', 'refund', 'invoice') THEN amount
                        WHEN type = 'consume' THEN -amount
                        ELSE 0
                    END
                )
                FROM nr_ledger_entries
                WHERE nr_ledger_entries.org_id = nr_orgs.id
            ),
            0
        )
        """
    )
    op.execute("UPDATE nr_orgs SET status = 'active', suspend_reason = NULL WHERE suspend_reason = 'payment_failed'")

    with op.batch_alter_table("nr_ledger_entries") as batch_op:
        batch_op.drop_constraint("ck_nr_ledger_type", type_="check")
        batch_op.create_check_constraint(
            "ck_nr_ledger_type",
            "type IN ('grant', 'consume', 'hold', 'release', 'refund', 'invoice', 'dispute')",
        )

    op.add_column(
        "nr_e_transfers",
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="CAD"),
    )
    op.add_column("nr_e_transfers", sa.Column("reference_normalized", sa.String(length=255), nullable=True))
    op.add_column("nr_e_transfers", sa.Column("fx_rate", sa.Numeric(18, 6), nullable=True))
    op.add_column("nr_e_transfers", sa.Column("fx_rate_date", sa.String(length=32), nullable=True))
    op.add_column("nr_e_transfers", sa.Column("fx_source", sa.String(length=64), nullable=True))
    op.add_column("nr_e_transfers", sa.Column("fx_margin", sa.Numeric(18, 6), nullable=True))
    op.add_column("nr_e_transfers", sa.Column("credited_usd", sa.Numeric(18, 6), nullable=True))

    op.execute("UPDATE nr_e_transfers SET reference_normalized = LOWER(TRIM(reference))")

    with op.batch_alter_table("nr_e_transfers") as batch_op:
        batch_op.drop_constraint("uq_nr_e_transfers_org_reference", type_="unique")
        batch_op.alter_column("reference_normalized", existing_type=sa.String(length=255), nullable=False)
    op.create_index(
        "uq_nr_e_transfers_reference_normalized",
        "nr_e_transfers",
        ["reference_normalized"],
        unique=True,
    )

    op.add_column("nr_subscriptions", sa.Column("price_id", sa.String(length=255), nullable=True))
    op.add_column("nr_subscriptions", sa.Column("last_event_created", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("nr_subscriptions") as batch_op:
        batch_op.drop_column("last_event_created")
        batch_op.drop_column("price_id")

    op.drop_index("uq_nr_e_transfers_reference_normalized", table_name="nr_e_transfers")
    with op.batch_alter_table("nr_e_transfers") as batch_op:
        batch_op.create_unique_constraint("uq_nr_e_transfers_org_reference", ["org_id", "reference"])
        batch_op.drop_column("credited_usd")
        batch_op.drop_column("fx_margin")
        batch_op.drop_column("fx_source")
        batch_op.drop_column("fx_rate_date")
        batch_op.drop_column("fx_rate")
        batch_op.drop_column("reference_normalized")
        batch_op.drop_column("currency")

    op.execute("DELETE FROM nr_ledger_entries WHERE type = 'dispute'")
    with op.batch_alter_table("nr_ledger_entries") as batch_op:
        batch_op.drop_constraint("ck_nr_ledger_type", type_="check")
        batch_op.create_check_constraint(
            "ck_nr_ledger_type",
            "type IN ('grant', 'consume', 'hold', 'release', 'refund', 'invoice')",
        )

    with op.batch_alter_table("nr_orgs") as batch_op:
        batch_op.drop_column("balance")
