"""sync browser profile and browser seed columns

Revision ID: 9c1f2a7be4d0
Revises: e4db575f75ee
Create Date: 2026-07-24 23:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "9c1f2a7be4d0"
down_revision: Union[str, None] = "e4db575f75ee"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("browser_profiles", sa.Column("last_verified_login_at", sa.DateTime(), nullable=True))
    op.add_column(
        "credentials",
        sa.Column("pin_saved_session_ip", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "uq_credentials_browser_profile_id",
        "credentials",
        ["browser_profile_id"],
        unique=True,
        postgresql_where=sa.text("browser_profile_id IS NOT NULL AND deleted_at IS NULL"),
    )
    op.add_column("workflow_runs", sa.Column("browser_seed_source", sa.String(), nullable=True))
    op.add_column("workflow_runs", sa.Column("browser_sink_profile_id", sa.String(), nullable=True))
    op.add_column("workflow_runs", sa.Column("start_fresh_browser", sa.Boolean(), nullable=True))
    op.create_index("idx_workflow_runs_wpid_created", "workflow_runs", ["workflow_permanent_id", "created_at"])


def downgrade() -> None:
    op.drop_index("idx_workflow_runs_wpid_created", table_name="workflow_runs")
    op.drop_column("workflow_runs", "start_fresh_browser")
    op.drop_column("workflow_runs", "browser_sink_profile_id")
    op.drop_column("workflow_runs", "browser_seed_source")
    op.drop_index("uq_credentials_browser_profile_id", table_name="credentials")
    op.drop_column("credentials", "pin_saved_session_ip")
    op.drop_column("browser_profiles", "last_verified_login_at")
