"""add per-number Stripe subscription billing lifecycle fields

Revision ID: 35f6e2470dd5
Revises: 1db436c126a8
Create Date: 2026-10-21T04:12:05.169595+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "35f6e2470dd5"
down_revision: Union[str, None] = "1db436c126a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organization_phone_numbers",
        sa.Column("stripe_subscription_id", sa.String(), nullable=True),
    )
    op.add_column(
        "organization_phone_numbers",
        sa.Column("stripe_subscription_item_id", sa.String(), nullable=True),
    )
    op.add_column(
        "organization_phone_numbers",
        sa.Column("stripe_payment_intent_id", sa.String(), nullable=True),
    )
    op.add_column(
        "organization_phone_numbers",
        sa.Column("stripe_invoice_id", sa.String(), nullable=True),
    )
    op.add_column(
        "organization_phone_numbers",
        sa.Column("stripe_refund_id", sa.String(), nullable=True),
    )
    op.add_column(
        "organization_phone_numbers",
        sa.Column("stripe_credit_note_id", sa.String(), nullable=True),
    )
    op.add_column(
        "organization_phone_numbers",
        sa.Column("current_period_start", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "organization_phone_numbers",
        sa.Column("current_period_end", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "organization_phone_numbers",
        sa.Column("cancel_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "organization_phone_numbers",
        sa.Column("maintenance_checked_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "organization_phone_numbers",
        sa.Column("purchase_generation", sa.String(), nullable=True),
    )
    # Exempt means not billed through Stripe: enterprise and pre-billing rows; new self-serve rows default false.
    op.add_column(
        "organization_phone_numbers",
        sa.Column("billing_exempt", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.alter_column(
        "organization_phone_numbers",
        "billing_exempt",
        server_default=sa.false(),
    )
    op.create_index(
        "uq_org_phone_numbers_stripe_subscription",
        "organization_phone_numbers",
        ["stripe_subscription_id"],
        unique=True,
        postgresql_where=sa.text("stripe_subscription_id IS NOT NULL AND deleted_at IS NULL"),
        sqlite_where=sa.text("stripe_subscription_id IS NOT NULL AND deleted_at IS NULL"),
    )
    op.drop_index("uq_org_phone_numbers_org_number", table_name="organization_phone_numbers")
    op.create_index(
        "uq_org_phone_numbers_org_number",
        "organization_phone_numbers",
        ["organization_id", "phone_number"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('active', 'provisioning', 'quarantined', 'quarantining', "
            "'releasing', 'surrendering', 'reversal_pending') AND deleted_at IS NULL"
        ),
        sqlite_where=sa.text(
            "status IN ('active', 'provisioning', 'quarantined', 'quarantining', "
            "'releasing', 'surrendering', 'reversal_pending') AND deleted_at IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_org_phone_numbers_org_number", table_name="organization_phone_numbers")
    op.create_index(
        "uq_org_phone_numbers_org_number",
        "organization_phone_numbers",
        ["organization_id", "phone_number"],
        unique=True,
        postgresql_where=sa.text("status IN ('active', 'provisioning', 'quarantined') AND deleted_at IS NULL"),
        sqlite_where=sa.text("status IN ('active', 'provisioning', 'quarantined') AND deleted_at IS NULL"),
    )
    op.drop_index("uq_org_phone_numbers_stripe_subscription", table_name="organization_phone_numbers")
    op.drop_column("organization_phone_numbers", "purchase_generation")
    op.drop_column("organization_phone_numbers", "billing_exempt")
    op.drop_column("organization_phone_numbers", "maintenance_checked_at")
    op.drop_column("organization_phone_numbers", "cancel_at")
    op.drop_column("organization_phone_numbers", "current_period_end")
    op.drop_column("organization_phone_numbers", "current_period_start")
    op.drop_column("organization_phone_numbers", "stripe_credit_note_id")
    op.drop_column("organization_phone_numbers", "stripe_refund_id")
    op.drop_column("organization_phone_numbers", "stripe_invoice_id")
    op.drop_column("organization_phone_numbers", "stripe_payment_intent_id")
    op.drop_column("organization_phone_numbers", "stripe_subscription_item_id")
    op.drop_column("organization_phone_numbers", "stripe_subscription_id")
