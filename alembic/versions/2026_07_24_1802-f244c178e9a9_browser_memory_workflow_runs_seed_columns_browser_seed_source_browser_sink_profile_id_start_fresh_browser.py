"""browser memory: workflow_runs seed columns (browser_seed_source, browser_sink_profile_id, start_fresh_browser)

Revision ID: f244c178e9a9
Revises: e4db575f75ee
Create Date: 2026-07-24T18:02:38.553861+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f244c178e9a9"
down_revision: Union[str, None] = "e4db575f75ee"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("workflow_runs", sa.Column("browser_seed_source", sa.String(), nullable=True))
    op.add_column("workflow_runs", sa.Column("browser_sink_profile_id", sa.String(), nullable=True))
    op.add_column("workflow_runs", sa.Column("start_fresh_browser", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("workflow_runs", "start_fresh_browser")
    op.drop_column("workflow_runs", "browser_sink_profile_id")
    op.drop_column("workflow_runs", "browser_seed_source")
