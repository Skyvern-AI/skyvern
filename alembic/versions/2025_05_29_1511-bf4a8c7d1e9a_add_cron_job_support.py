"""Add cron job support

Revision ID: bf4a8c7d1e9a
Revises: af49ca791fc7
Create Date: 2025-05-29 15:11:10.000000+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bf4a8c7d1e9a"
down_revision: Union[str, None] = "af49ca791fc7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add cron job support fields to workflows table
    op.add_column(
        "workflows", 
        sa.Column("cron_expression", sa.String(), nullable=True)
    )
    op.add_column(
        "workflows", 
        sa.Column("timezone", sa.String(), nullable=True, server_default="UTC")
    )
    op.add_column(
        "workflows", 
        sa.Column("cron_enabled", sa.Boolean(), nullable=False, server_default="false")
    )
    op.add_column(
        "workflows", 
        sa.Column("next_run_time", sa.DateTime(), nullable=True)
    )
    op.create_index(
        "workflow_next_run_time_idx", 
        "workflows", 
        ["next_run_time"]
    )
    
    # Add triggered_by_cron field to workflow_runs table
    op.add_column(
        "workflow_runs", 
        sa.Column("triggered_by_cron", sa.Boolean(), nullable=False, server_default="false")
    )


def downgrade() -> None:
    # Remove triggered_by_cron field from workflow_runs table
    op.drop_column("workflow_runs", "triggered_by_cron")
    
    # Remove cron job support fields from workflows table
    op.drop_index("workflow_next_run_time_idx", table_name="workflows")
    op.drop_column("workflows", "next_run_time")
    op.drop_column("workflows", "cron_enabled")
    op.drop_column("workflows", "timezone")
    op.drop_column("workflows", "cron_expression")
