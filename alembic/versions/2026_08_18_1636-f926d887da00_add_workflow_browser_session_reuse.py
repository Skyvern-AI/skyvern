"""add workflow browser session reuse

Revision ID: f926d887da00
Revises: 3836bc93841d
Create Date: 2026-08-18T16:36:02.888566+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f926d887da00"
down_revision: Union[str, None] = "3836bc93841d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE workflows ADD COLUMN IF NOT EXISTS reuse_browser_session BOOLEAN NOT NULL DEFAULT FALSE")
    op.execute("ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS reuse_browser_session BOOLEAN")
    op.execute("ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS reuse_bound_key VARCHAR")


def downgrade() -> None:
    op.execute("ALTER TABLE workflow_runs DROP COLUMN IF EXISTS reuse_bound_key")
    op.execute("ALTER TABLE workflow_runs DROP COLUMN IF EXISTS reuse_browser_session")
    op.execute("ALTER TABLE workflows DROP COLUMN IF EXISTS reuse_browser_session")
