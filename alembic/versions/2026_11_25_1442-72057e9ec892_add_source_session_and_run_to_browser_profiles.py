"""add source session and run to browser profiles

Revision ID: 72057e9ec892
Revises: d564698fe8a2
Create Date: 2026-11-25T14:42:15.410400+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "72057e9ec892"
down_revision: Union[str, None] = "d564698fe8a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("browser_profiles", sa.Column("source_browser_session_id", sa.String(), nullable=True))
    op.add_column("browser_profiles", sa.Column("source_workflow_run_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("browser_profiles", "source_workflow_run_id")
    op.drop_column("browser_profiles", "source_browser_session_id")
