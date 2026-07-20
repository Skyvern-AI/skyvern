"""add task title to eval browser session results

Revision ID: 0e5e3831d7d0
Revises: e4db575f75ee
Create Date: 2026-07-20T18:52:57.635838+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0e5e3831d7d0"
down_revision: Union[str, None] = "e4db575f75ee"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("eval_browser_session_results", sa.Column("task_title", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("eval_browser_session_results", "task_title")
