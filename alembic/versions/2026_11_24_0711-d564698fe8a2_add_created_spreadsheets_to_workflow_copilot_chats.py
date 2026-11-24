"""add created_spreadsheets to workflow copilot chats

Revision ID: d564698fe8a2
Revises: 738dfed1bb9b
Create Date: 2026-11-24T07:11:25.471678+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d564698fe8a2"
down_revision: Union[str, None] = "738dfed1bb9b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("workflow_copilot_chats", sa.Column("created_spreadsheets", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workflow_copilot_chats", "created_spreadsheets")
