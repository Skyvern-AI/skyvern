"""add feedback columns to workflow_copilot_chat_messages

Revision ID: 1ee65f3f74f8
Revises: 1bc01bfed6bf
Create Date: 2026-09-25T05:56:18.609508+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1ee65f3f74f8"
down_revision: Union[str, None] = "1bc01bfed6bf"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("workflow_copilot_chat_messages", sa.Column("feedback_rating", sa.String(), nullable=True))
    op.add_column("workflow_copilot_chat_messages", sa.Column("feedback_reason", sa.UnicodeText(), nullable=True))
    op.add_column("workflow_copilot_chat_messages", sa.Column("feedback_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workflow_copilot_chat_messages", "feedback_at")
    op.drop_column("workflow_copilot_chat_messages", "feedback_reason")
    op.drop_column("workflow_copilot_chat_messages", "feedback_rating")
