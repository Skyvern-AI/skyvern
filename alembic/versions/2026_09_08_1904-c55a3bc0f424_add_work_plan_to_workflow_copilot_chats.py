"""add work_plan to workflow_copilot_chats

Revision ID: c55a3bc0f424
Revises: 456c370b0f7d
Create Date: 2026-09-08T19:04:59.010908+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c55a3bc0f424"
down_revision: Union[str, None] = "456c370b0f7d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("workflow_copilot_chats", sa.Column("work_plan", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workflow_copilot_chats", "work_plan")
