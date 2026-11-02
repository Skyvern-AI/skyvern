"""add agent_feedback table

Revision ID: 5112f44a9e61
Revises: 35f6e2470dd5
Create Date: 2026-10-21T19:42:03.217149+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5112f44a9e61"
down_revision: Union[str, None] = "35f6e2470dd5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_feedback",
        sa.Column("agent_feedback_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=True),
        sa.Column("summary", sa.UnicodeText(), nullable=False),
        sa.Column("details", sa.UnicodeText(), nullable=True),
        sa.Column("tool_name", sa.String(), nullable=True),
        sa.Column("context", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.organization_id"]),
        sa.PrimaryKeyConstraint("agent_feedback_id"),
    )
    op.create_index(
        "ix_agent_feedback_org_created_at", "agent_feedback", ["organization_id", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_agent_feedback_org_created_at", table_name="agent_feedback")
    op.drop_table("agent_feedback")
