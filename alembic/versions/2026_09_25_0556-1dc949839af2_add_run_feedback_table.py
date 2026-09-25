"""add run_feedback table

Revision ID: 1dc949839af2
Revises: 1ee65f3f74f8
Create Date: 2026-09-25T05:56:18.610567+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1dc949839af2"
down_revision: Union[str, None] = "1ee65f3f74f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "run_feedback",
        sa.Column("run_feedback_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("target_type", sa.String(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("context_id", sa.String(), nullable=True),
        sa.Column("rating", sa.String(), nullable=False),
        sa.Column("reason", sa.UnicodeText(), nullable=True),
        sa.Column("needs_support", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("submitted_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.organization_id"]),
        sa.PrimaryKeyConstraint("run_feedback_id"),
    )
    op.create_index("ix_run_feedback_organization_id", "run_feedback", ["organization_id"], unique=False)
    op.create_index(
        "ux_run_feedback_org_target", "run_feedback", ["organization_id", "target_type", "target_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ux_run_feedback_org_target", table_name="run_feedback")
    op.drop_index("ix_run_feedback_organization_id", table_name="run_feedback")
    op.drop_table("run_feedback")
