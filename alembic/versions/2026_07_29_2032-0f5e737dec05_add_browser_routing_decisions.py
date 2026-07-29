"""add browser_routing_decisions

Revision ID: 0f5e737dec05
Revises: 1fe4efc6b752
Create Date: 2026-07-29T20:32:17.615744+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0f5e737dec05"
down_revision: Union[str, None] = "1fe4efc6b752"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "browser_routing_decisions",
        sa.Column("browser_routing_decision_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=True),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("workflow_run_id", sa.String(), nullable=True),
        sa.Column("requested_browser_family", sa.String(), nullable=False),
        sa.Column("dispatched_browser_family", sa.String(), nullable=False),
        sa.Column("compliance_profile_key", sa.String(), nullable=True),
        sa.Column("data_class", sa.String(), nullable=False),
        sa.Column("data_class_source", sa.String(), nullable=False),
        sa.Column("allowed", sa.Boolean(), nullable=False),
        sa.Column("compliance_basis", sa.String(), nullable=False),
        sa.Column("declared_handling", sa.String(), nullable=True),
        sa.Column("handling_exception_reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("browser_routing_decision_id"),
    )
    op.create_index(
        "ix_browser_routing_decisions_org_created",
        "browser_routing_decisions",
        ["organization_id", "created_at"],
    )
    op.create_index(
        "ix_browser_routing_decisions_workflow_run_id",
        "browser_routing_decisions",
        ["workflow_run_id"],
    )
    op.create_index(
        "ix_browser_routing_decisions_task_id",
        "browser_routing_decisions",
        ["task_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_browser_routing_decisions_task_id", table_name="browser_routing_decisions")
    op.drop_index("ix_browser_routing_decisions_workflow_run_id", table_name="browser_routing_decisions")
    op.drop_index("ix_browser_routing_decisions_org_created", table_name="browser_routing_decisions")
    op.drop_table("browser_routing_decisions")
