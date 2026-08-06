"""add pending_turns to workflow_copilot_chats

Revision ID: 92ebe14532b9
Revises: 9d85c51a8468
Create Date: 2026-07-31T16:34:24.245309+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "92ebe14532b9"
down_revision: Union[str, None] = "9d85c51a8468"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("workflow_copilot_chats", sa.Column("pending_turns", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("workflow_copilot_chats", "pending_turns")
