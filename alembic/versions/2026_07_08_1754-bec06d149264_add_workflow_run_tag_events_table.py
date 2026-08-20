"""add workflow run tag events table

Revision ID: bec06d149264
Revises: 5b618ec6dce6
Create Date: 2026-07-08T17:54:31.712270+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bec06d149264"
down_revision: Union[str, None] = "5b618ec6dce6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workflow_run_tag_events",
        sa.Column("tag_event_id", sa.String(), nullable=False),
        sa.Column("workflow_run_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=True),
        sa.Column("value", sa.String(), nullable=True),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("set_at", sa.DateTime(), nullable=False),
        sa.Column("set_by", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("caller_type", sa.String(), nullable=True),
        sa.Column("superseded_at", sa.DateTime(), nullable=True),
        sa.Column("inherited_from_tag_event_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("event_type IN ('set', 'delete')", name="ck_workflow_run_tag_events_event_type"),
        sa.CheckConstraint(
            "source IN ('manual', 'bulk_apply', 'backfill', 'inherited', 'import', 'system')",
            name="ck_workflow_run_tag_events_source",
        ),
        sa.CheckConstraint(
            "caller_type IS NULL OR caller_type IN ('user', 'api_key', 'system')",
            name="ck_workflow_run_tag_events_caller_type",
        ),
        sa.CheckConstraint(
            "event_type != 'set' OR value IS NOT NULL",
            name="ck_workflow_run_tag_events_set_has_value",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_run_id"],
            ["workflow_runs.workflow_run_id"],
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.organization_id"],
        ),
        sa.PrimaryKeyConstraint("tag_event_id"),
    )
    op.create_index(
        "workflow_run_tag_events_org_wr_set_at_idx",
        "workflow_run_tag_events",
        ["organization_id", "workflow_run_id", "set_at"],
        unique=False,
    )
    op.create_index(
        "workflow_run_tag_events_org_key_value_active_idx",
        "workflow_run_tag_events",
        ["organization_id", "key", "value"],
        unique=False,
        postgresql_include=["workflow_run_id"],
        postgresql_where=sa.text("superseded_at IS NULL AND event_type = 'set'"),
    )
    op.create_index(
        "workflow_run_tag_events_org_value_active_idx",
        "workflow_run_tag_events",
        ["organization_id", "value"],
        unique=False,
        postgresql_include=["workflow_run_id"],
        postgresql_where=sa.text("superseded_at IS NULL AND event_type = 'set'"),
    )
    op.create_index(
        "workflow_run_tag_events_org_set_at_idx",
        "workflow_run_tag_events",
        ["organization_id", "set_at"],
        unique=False,
    )
    op.create_index(
        "workflow_run_tag_events_active_grouped_unique",
        "workflow_run_tag_events",
        ["organization_id", "workflow_run_id", "key"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL AND event_type = 'set' AND key IS NOT NULL"),
        sqlite_where=sa.text("superseded_at IS NULL AND event_type = 'set' AND key IS NOT NULL"),
    )
    op.create_index(
        "workflow_run_tag_events_active_label_unique",
        "workflow_run_tag_events",
        ["organization_id", "workflow_run_id", "value"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL AND event_type = 'set' AND key IS NULL"),
        sqlite_where=sa.text("superseded_at IS NULL AND event_type = 'set' AND key IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "workflow_run_tag_events_active_label_unique",
        table_name="workflow_run_tag_events",
        postgresql_where=sa.text("superseded_at IS NULL AND event_type = 'set' AND key IS NULL"),
    )
    op.drop_index(
        "workflow_run_tag_events_active_grouped_unique",
        table_name="workflow_run_tag_events",
        postgresql_where=sa.text("superseded_at IS NULL AND event_type = 'set' AND key IS NOT NULL"),
    )
    op.drop_index("workflow_run_tag_events_org_set_at_idx", table_name="workflow_run_tag_events")
    op.drop_index(
        "workflow_run_tag_events_org_value_active_idx",
        table_name="workflow_run_tag_events",
        postgresql_include=["workflow_run_id"],
        postgresql_where=sa.text("superseded_at IS NULL AND event_type = 'set'"),
    )
    op.drop_index(
        "workflow_run_tag_events_org_key_value_active_idx",
        table_name="workflow_run_tag_events",
        postgresql_include=["workflow_run_id"],
        postgresql_where=sa.text("superseded_at IS NULL AND event_type = 'set'"),
    )
    op.drop_index("workflow_run_tag_events_org_wr_set_at_idx", table_name="workflow_run_tag_events")
    op.drop_table("workflow_run_tag_events")
