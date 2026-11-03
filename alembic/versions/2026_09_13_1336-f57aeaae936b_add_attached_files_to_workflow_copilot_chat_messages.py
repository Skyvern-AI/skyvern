"""add attached_files to workflow_copilot_chat_messages

Revision ID: f57aeaae936b
Revises: dbc92ed4a237
Create Date: 2026-09-13T13:36:32.282689+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f57aeaae936b"
down_revision: Union[str, None] = "dbc92ed4a237"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("workflow_copilot_chat_messages", sa.Column("attached_files", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workflow_copilot_chat_messages", "attached_files")
