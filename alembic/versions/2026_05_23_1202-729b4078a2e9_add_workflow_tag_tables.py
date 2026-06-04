"""add workflow tag tables

Revision ID: 729b4078a2e9
Revises: d7e8f9a0b1c2
Create Date: 2026-05-23T12:02:15.280583+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "729b4078a2e9"
down_revision: Union[str, None] = "d7e8f9a0b1c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tag_keys",
        sa.Column("tag_key_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.organization_id"],
        ),
        sa.PrimaryKeyConstraint("tag_key_id"),
    )
    op.create_index(
        "ix_tag_keys_org_key_active",
        "tag_keys",
        ["organization_id", "key"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_table(
        "workflow_tag_events",
        sa.Column("tag_event_id", sa.String(), nullable=False),
        sa.Column("workflow_permanent_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=True),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("set_at", sa.DateTime(), nullable=False),
        sa.Column("set_by", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("superseded_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("event_type != 'delete' OR value IS NULL", name="ck_workflow_tag_events_delete_null_value"),
        sa.CheckConstraint("event_type IN ('set', 'delete')", name="ck_workflow_tag_events_event_type"),
        sa.CheckConstraint(
            "source IN ('manual', 'bulk_apply', 'backfill', 'inherited', 'import')",
            name="ck_workflow_tag_events_source",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.organization_id"],
        ),
        sa.PrimaryKeyConstraint("tag_event_id"),
    )
    op.create_index(
        "workflow_tag_events_active_set_unique",
        "workflow_tag_events",
        ["organization_id", "workflow_permanent_id", "key"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL AND event_type = 'set'"),
    )
    op.create_index(
        "workflow_tag_events_org_key_value_active_idx",
        "workflow_tag_events",
        ["organization_id", "key", "value"],
        unique=False,
        postgresql_include=["workflow_permanent_id"],
        postgresql_where=sa.text("superseded_at IS NULL AND event_type = 'set'"),
    )
    op.create_index(
        "workflow_tag_events_org_set_at_idx", "workflow_tag_events", ["organization_id", "set_at"], unique=False
    )
    op.create_index(
        "workflow_tag_events_org_wpid_key_set_at_idx",
        "workflow_tag_events",
        ["organization_id", "workflow_permanent_id", "key", "set_at"],
        unique=False,
    )
    op.create_index(
        "workflow_tag_events_org_wpid_set_at_idx",
        "workflow_tag_events",
        ["organization_id", "workflow_permanent_id", "set_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("workflow_tag_events_org_wpid_set_at_idx", table_name="workflow_tag_events")
    op.drop_index("workflow_tag_events_org_wpid_key_set_at_idx", table_name="workflow_tag_events")
    op.drop_index("workflow_tag_events_org_set_at_idx", table_name="workflow_tag_events")
    op.drop_index(
        "workflow_tag_events_org_key_value_active_idx",
        table_name="workflow_tag_events",
        postgresql_include=["workflow_permanent_id"],
        postgresql_where=sa.text("superseded_at IS NULL AND event_type = 'set'"),
    )
    op.drop_index(
        "workflow_tag_events_active_set_unique",
        table_name="workflow_tag_events",
        postgresql_where=sa.text("superseded_at IS NULL AND event_type = 'set'"),
    )
    op.drop_table("workflow_tag_events")
    op.drop_index("ix_tag_keys_org_key_active", table_name="tag_keys", postgresql_where=sa.text("deleted_at IS NULL"))
    op.drop_table("tag_keys")
