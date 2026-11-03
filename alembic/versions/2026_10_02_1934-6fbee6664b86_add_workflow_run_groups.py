"""add workflow run groups

Revision ID: 6fbee6664b86
Revises: 13139662a50b
Create Date: 2026-10-02T19:34:31.499073+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6fbee6664b86"
down_revision: Union[str, None] = "13139662a50b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workflow_run_groups",
        sa.Column("workflow_run_group_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("workflow_permanent_id", sa.String(), nullable=False),
        sa.Column("requested_version", sa.Integer(), nullable=True),
        sa.Column("workflow_id", sa.String(), nullable=False),
        sa.Column("submission_key", sa.String(), nullable=False),
        sa.Column("input_fingerprint", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("workflow_run_group_id"),
        sa.UniqueConstraint("organization_id", "submission_key", name="uq_workflow_run_groups_org_submission_key"),
    )
    op.create_index(
        "idx_workflow_run_groups_unfinished_modified_at",
        "workflow_run_groups",
        ["modified_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('active', 'cancel_requested')"),
    )
    op.create_table(
        "workflow_run_group_items",
        sa.Column("workflow_run_group_id", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("item_key", sa.String(), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("workflow_run_id", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("dispatch_token", sa.String(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("dispatch_attempts", sa.Integer(), nullable=False),
        sa.Column("failure_reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("workflow_run_group_id", "position"),
        sa.UniqueConstraint("workflow_run_group_id", "item_key", name="uq_workflow_run_group_items_group_item_key"),
        sa.UniqueConstraint("workflow_run_id", name="uq_workflow_run_group_items_workflow_run_id"),
    )


def downgrade() -> None:
    op.drop_table("workflow_run_group_items")
    op.drop_index(
        "idx_workflow_run_groups_unfinished_modified_at",
        table_name="workflow_run_groups",
        postgresql_where=sa.text("status IN ('active', 'cancel_requested')"),
    )
    op.drop_table("workflow_run_groups")
