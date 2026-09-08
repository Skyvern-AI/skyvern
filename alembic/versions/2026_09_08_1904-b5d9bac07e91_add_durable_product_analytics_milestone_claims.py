"""add durable product analytics milestone claims

Revision ID: b5d9bac07e91
Revises: c55a3bc0f424
Create Date: 2026-09-08T19:04:59.011380+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b5d9bac07e91"
down_revision: Union[str, None] = "c55a3bc0f424"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "product_analytics_milestones",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("milestone_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("organization_id", "milestone_key"),
    )
    op.execute(
        sa.text(
            "INSERT INTO product_analytics_milestones "
            "(organization_id, milestone_key, created_at) "
            "VALUES ('*', 'installed_at', now())"
        )
    )


def downgrade() -> None:
    op.drop_table("product_analytics_milestones")
