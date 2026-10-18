"""add workflow_runs.requested_by, workflows.edited_for and persistent_browser_sessions.created_for_workflow_permanent_id

Revision ID: 38ab1789a9b1
Revises: 7dc8dbe57b56
Create Date: 2026-10-18T05:18:54.485808+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "38ab1789a9b1"
down_revision: Union[str, None] = "7dc8dbe57b56"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # workflows first: the workflow_runs lock is then held only from its ALTER to commit.
    op.add_column("workflows", sa.Column("edited_for", sa.String(), nullable=True))
    op.add_column(
        "persistent_browser_sessions", sa.Column("created_for_workflow_permanent_id", sa.String(), nullable=True)
    )
    op.add_column("workflow_runs", sa.Column("requested_by", sa.String(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workflows", "edited_for")
    op.drop_column("persistent_browser_sessions", "created_for_workflow_permanent_id")
    op.drop_column("workflow_runs", "requested_by")
