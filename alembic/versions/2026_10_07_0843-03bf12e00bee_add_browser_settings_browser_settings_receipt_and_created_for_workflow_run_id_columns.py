"""add browser_settings, browser_settings_receipt and created_for_workflow_run_id columns

Revision ID: 03bf12e00bee
Revises: 8f3cbbcbbf34
Create Date: 2026-10-07T08:43:41.943920+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "03bf12e00bee"
down_revision: Union[str, None] = "8f3cbbcbbf34"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("persistent_browser_sessions", sa.Column("browser_settings", sa.JSON(), nullable=True))
    op.add_column("persistent_browser_sessions", sa.Column("browser_settings_receipt", sa.JSON(), nullable=True))
    op.add_column("persistent_browser_sessions", sa.Column("created_for_workflow_run_id", sa.String(), nullable=True))
    op.add_column("workflow_runs", sa.Column("browser_settings", sa.JSON(), nullable=True))
    op.add_column("workflow_runs", sa.Column("browser_settings_receipt", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workflow_runs", "browser_settings_receipt")
    op.drop_column("workflow_runs", "browser_settings")
    op.drop_column("persistent_browser_sessions", "created_for_workflow_run_id")
    op.drop_column("persistent_browser_sessions", "browser_settings_receipt")
    op.drop_column("persistent_browser_sessions", "browser_settings")
