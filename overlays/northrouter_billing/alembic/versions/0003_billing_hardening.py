"""Add billing state and lookup constraints."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_billing_hardening"
down_revision: Union[str, None] = "0002_hold_expiry"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("nr_orgs", sa.Column("suspend_reason", sa.String(length=64), nullable=True))
    op.execute("UPDATE nr_orgs SET suspend_reason='payment_failed' WHERE status='suspended' AND suspend_reason IS NULL")
    op.create_index("uq_nr_orgs_stripe_customer_id", "nr_orgs", ["stripe_customer_id"], unique=True)
    op.create_index("ix_nr_ledger_org_type", "nr_ledger_entries", ["org_id", "type"])
    with op.batch_alter_table("nr_e_transfers") as batch_op:
        batch_op.create_unique_constraint("uq_nr_e_transfers_org_reference", ["org_id", "reference"])
    op.drop_index("ix_nr_ledger_entries_org_id", table_name="nr_ledger_entries")
    op.drop_index("ix_nr_holds_org_id", table_name="nr_holds")


def downgrade() -> None:
    op.create_index("ix_nr_holds_org_id", "nr_holds", ["org_id"])
    op.create_index("ix_nr_ledger_entries_org_id", "nr_ledger_entries", ["org_id"])
    with op.batch_alter_table("nr_e_transfers") as batch_op:
        batch_op.drop_constraint("uq_nr_e_transfers_org_reference", type_="unique")
    op.drop_index("ix_nr_ledger_org_type", table_name="nr_ledger_entries")
    op.drop_index("uq_nr_orgs_stripe_customer_id", table_name="nr_orgs")
    op.drop_column("nr_orgs", "suspend_reason")
