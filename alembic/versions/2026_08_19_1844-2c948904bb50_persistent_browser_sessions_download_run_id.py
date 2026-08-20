"""persistent_browser_sessions.download_run_id

Revision ID: 2c948904bb50
Revises: 49bb8a1289de
Create Date: 2026-08-19T18:44:56.704271+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2c948904bb50"
down_revision: Union[str, None] = "49bb8a1289de"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("persistent_browser_sessions", sa.Column("download_run_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("persistent_browser_sessions", "download_run_id")
