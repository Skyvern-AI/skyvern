"""add_adaptive_caching_tables

Revision ID: 700dfebe7c5f
Revises: dc37d888db44
Create Date: 2026-02-27 12:00:00.000000+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "700dfebe7c5f"
down_revision: Union[str, None] = "dc37d888db44"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add adaptive_caching and generate_script_on_terminal columns to workflows table
    op.add_column(
        "workflows",
        sa.Column("adaptive_caching", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "workflows",
        sa.Column("generate_script_on_terminal", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    # Add requires_agent column to script_blocks table
    op.add_column(
        "script_blocks",
        sa.Column("requires_agent", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    # Create script_fallback_episodes table
    op.create_table(
        "script_fallback_episodes",
        sa.Column("episode_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("workflow_permanent_id", sa.String(), nullable=False),
        sa.Column("workflow_run_id", sa.String(), nullable=False),
        sa.Column("script_revision_id", sa.String(), nullable=True),
        sa.Column("block_label", sa.String(), nullable=False),
        sa.Column("fallback_type", sa.String(), nullable=False),
        sa.Column("error_message", sa.UnicodeText(), nullable=True),
        sa.Column("classify_result", sa.String(), nullable=True),
        sa.Column("agent_actions", sa.JSON(), nullable=True),
        sa.Column("page_url", sa.String(), nullable=True),
        sa.Column("page_text_snapshot", sa.UnicodeText(), nullable=True),
        sa.Column("fallback_succeeded", sa.Boolean(), nullable=True),
        sa.Column("reviewed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reviewer_output", sa.UnicodeText(), nullable=True),
        sa.Column("new_script_revision_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("episode_id"),
    )
    op.create_index(
        "sfe_org_wpid_index",
        "script_fallback_episodes",
        ["organization_id", "workflow_permanent_id"],
    )
    op.create_index(
        "sfe_org_created_at_index",
        "script_fallback_episodes",
        ["organization_id", "created_at"],
    )

    # Create script_branch_hits table
    op.create_table(
        "script_branch_hits",
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("workflow_permanent_id", sa.String(), nullable=False),
        sa.Column("block_label", sa.String(), nullable=False),
        sa.Column("branch_key", sa.String(), nullable=False),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("first_hit_at", sa.DateTime(), nullable=False),
        sa.Column("last_hit_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("organization_id", "workflow_permanent_id", "block_label", "branch_key"),
    )
    op.create_index(
        "sbh_org_wpid_index",
        "script_branch_hits",
        ["organization_id", "workflow_permanent_id"],
    )


def downgrade() -> None:
    op.drop_index("sbh_org_wpid_index", table_name="script_branch_hits")
    op.drop_table("script_branch_hits")
    op.drop_index("sfe_org_created_at_index", table_name="script_fallback_episodes")
    op.drop_index("sfe_org_wpid_index", table_name="script_fallback_episodes")
    op.drop_table("script_fallback_episodes")
    op.drop_column("script_blocks", "requires_agent")
    op.drop_column("workflows", "generate_script_on_terminal")
    op.drop_column("workflows", "adaptive_caching")
