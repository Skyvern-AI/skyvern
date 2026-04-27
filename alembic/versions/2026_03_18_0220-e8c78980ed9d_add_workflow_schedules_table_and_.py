"""add workflow_schedules table and trigger_type to workflow_runs

Revision ID: e8c78980ed9d
Revises: def7c03f425d
Create Date: 2026-03-18 02:20:36.978880+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e8c78980ed9d"
down_revision: Union[str, None] = "def7c03f425d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workflow_schedules",
        sa.Column("workflow_schedule_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("workflow_permanent_id", sa.String(), nullable=False),
        sa.Column("cron_expression", sa.String(), nullable=False),
        sa.Column("timezone", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("parameters", sa.JSON(), nullable=True),
        sa.Column("temporal_schedule_id", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("modified_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("workflow_schedule_id"),
    )
    op.create_index(
        "idx_workflow_schedules_org_workflow",
        "workflow_schedules",
        ["organization_id", "workflow_permanent_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "idx_workflow_schedules_org_enabled",
        "workflow_schedules",
        ["organization_id", "enabled"],
    )
    op.create_index(
        op.f("ix_workflow_schedules_workflow_permanent_id"),
        "workflow_schedules",
        ["workflow_permanent_id"],
    )

    # Add trigger_type and workflow_schedule_id columns to workflow_runs
    op.add_column(
        "workflow_runs",
        sa.Column("trigger_type", sa.String(), nullable=True),
    )
    op.add_column(
        "workflow_runs",
        sa.Column("workflow_schedule_id", sa.String(), nullable=True),
    )
    op.create_index(
        op.f("ix_workflow_runs_workflow_schedule_id"),
        "workflow_runs",
        ["workflow_schedule_id"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_workflow_runs_workflow_schedule_id"), table_name="workflow_runs")
    op.drop_column("workflow_runs", "workflow_schedule_id")
    op.drop_column("workflow_runs", "trigger_type")

    op.drop_index("idx_workflow_schedules_org_workflow", table_name="workflow_schedules")
    op.drop_index(op.f("ix_workflow_schedules_workflow_permanent_id"), table_name="workflow_schedules")
    op.drop_index("idx_workflow_schedules_org_enabled", table_name="workflow_schedules")
    op.drop_table("workflow_schedules")
