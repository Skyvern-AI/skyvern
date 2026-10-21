"""add task_queue to tasks

Revision ID: 7e92edcb0f3b
Revises: c27d5b17c62d
Create Date: 2026-10-18T05:18:54.489039+00:00

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7e92edcb0f3b"
down_revision: Union[str, None] = "c27d5b17c62d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS task_queue VARCHAR")
        op.execute("RESET lock_timeout")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS task_queue")
        op.execute("RESET lock_timeout")
