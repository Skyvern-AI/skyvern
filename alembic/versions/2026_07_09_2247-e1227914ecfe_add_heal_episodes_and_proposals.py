"""add heal episodes and proposals

Revision ID: e1227914ecfe
Revises: bec06d149264
Create Date: 2026-07-09T22:47:11.311011+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1227914ecfe"
down_revision: Union[str, None] = "bec06d149264"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "heal_episodes",
        sa.Column("heal_episode_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("workflow_permanent_id", sa.String(), nullable=False),
        sa.Column("workflow_id", sa.String(), nullable=False),
        sa.Column("workflow_run_id", sa.String(), nullable=False),
        sa.Column("workflow_run_block_id", sa.String(), nullable=False),
        sa.Column("block_label", sa.String(), nullable=False),
        sa.Column("engine", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("skip_reason", sa.String(), nullable=True),
        sa.Column("block_prompt", sa.UnicodeText(), nullable=True),
        sa.Column("block_code", sa.UnicodeText(), nullable=True),
        sa.Column("block_steps", sa.JSON(), nullable=True),
        sa.Column("snapshot_available", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("convergence_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("parameter_binding_keys", sa.JSON(), nullable=True),
        sa.Column("exception_class", sa.String(), nullable=True),
        sa.Column("failing_line", sa.Integer(), nullable=True),
        sa.Column("matched_step_index", sa.Integer(), nullable=True),
        sa.Column("failure_message", sa.UnicodeText(), nullable=True),
        sa.Column("escalation_task_id", sa.String(), nullable=True),
        sa.Column("wall_clock_ms", sa.Integer(), nullable=True),
        sa.Column("action_count", sa.Integer(), nullable=True),
        sa.Column("output_obligation", sa.String(), nullable=True),
        sa.Column("dom_snapshot_artifact_id", sa.String(), nullable=True),
        sa.Column("scout_transcript_artifact_id", sa.String(), nullable=True),
        sa.Column("screenshot_artifact_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("heal_episode_id"),
    )
    op.create_index(
        "he_org_wpid_index",
        "heal_episodes",
        ["organization_id", "workflow_permanent_id", "created_at"],
        unique=False,
    )
    op.create_index("he_org_created_at_index", "heal_episodes", ["organization_id", "created_at"], unique=False)
    op.create_index("he_org_wrid_index", "heal_episodes", ["organization_id", "workflow_run_id"], unique=False)

    op.create_table(
        "workflow_heal_proposals",
        sa.Column("heal_proposal_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("workflow_permanent_id", sa.String(), nullable=False),
        sa.Column("block_label", sa.String(), nullable=False),
        sa.Column("candidate_definition", sa.JSON(), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("episode_ids", sa.JSON(), nullable=False),
        sa.Column("rendered_diff", sa.UnicodeText(), nullable=True),
        sa.Column("base_version", sa.Integer(), nullable=False),
        sa.Column("base_definition_hash", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default=sa.text("'proposed'")),
        sa.Column("adopted_workflow_id", sa.String(), nullable=True),
        sa.Column("episode_window", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("heal_proposal_id"),
    )
    op.create_index(
        "hp_org_wpid_index",
        "workflow_heal_proposals",
        ["organization_id", "workflow_permanent_id"],
        unique=False,
    )

    op.add_column(
        "organizations",
        sa.Column("selfheal_screenshot_capture_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("organizations", sa.Column("selfheal_artifact_retention_days", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("organizations", "selfheal_artifact_retention_days")
    op.drop_column("organizations", "selfheal_screenshot_capture_enabled")

    op.drop_index("hp_org_wpid_index", table_name="workflow_heal_proposals")
    op.drop_table("workflow_heal_proposals")

    op.drop_index("he_org_wrid_index", table_name="heal_episodes")
    op.drop_index("he_org_created_at_index", table_name="heal_episodes")
    op.drop_index("he_org_wpid_index", table_name="heal_episodes")
    op.drop_table("heal_episodes")
