"""add workflow run routing

Revision ID: 317a80337f2e
Revises: e5e072231239
Create Date: 2026-09-21T18:43:50.129051+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "317a80337f2e"
down_revision: Union[str, None] = "e5e072231239"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("workflow_runs", sa.Column("task_queue", sa.String(), nullable=True))
    op.add_column("workflow_runs", sa.Column("target_cluster", sa.String(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workflow_runs", "target_cluster")
    op.drop_column("workflow_runs", "task_queue")
