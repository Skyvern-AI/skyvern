"""tagging: key-optional labels (nullable key, dual partial-unique)

Revision ID: 4dbd183edee0
Revises: 8266bacba614
Create Date: 2026-06-09T16:34:35.143445+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4dbd183edee0"
down_revision: Union[str, None] = "8266bacba614"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("workflow_tag_events", "key", existing_type=sa.String(), nullable=True)

    # The old per-(org, wpid, key) uniqueness can't express a group-less label.
    # Split it: grouped unique per key, standalone unique per value.
    op.drop_index(
        "workflow_tag_events_active_set_unique",
        table_name="workflow_tag_events",
        postgresql_where=sa.text("superseded_at IS NULL AND event_type = 'set'"),
    )
    op.create_index(
        "workflow_tag_events_active_grouped_unique",
        "workflow_tag_events",
        ["organization_id", "workflow_permanent_id", "key"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL AND event_type = 'set' AND key IS NOT NULL"),
    )
    op.create_index(
        "workflow_tag_events_active_label_unique",
        "workflow_tag_events",
        ["organization_id", "workflow_permanent_id", "value"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL AND event_type = 'set' AND key IS NULL"),
    )
    # Supports the value-only ("filter by label") term, which matches a value
    # across any/no group.
    op.create_index(
        "workflow_tag_events_org_value_active_idx",
        "workflow_tag_events",
        ["organization_id", "value"],
        unique=False,
        postgresql_include=["workflow_permanent_id"],
        postgresql_where=sa.text("superseded_at IS NULL AND event_type = 'set'"),
    )

    # DELETE rows may now carry a value (standalone-label deletes); only SET rows
    # must have a value. Replaces the old "delete rows have null value" constraint.
    op.drop_constraint("ck_workflow_tag_events_delete_null_value", "workflow_tag_events", type_="check")
    op.create_check_constraint(
        "ck_workflow_tag_events_set_has_value",
        "workflow_tag_events",
        "event_type != 'set' OR value IS NOT NULL",
    )


def downgrade() -> None:
    op.execute("DELETE FROM workflow_tag_events WHERE key IS NULL")
    op.drop_constraint("ck_workflow_tag_events_set_has_value", "workflow_tag_events", type_="check")
    op.create_check_constraint(
        "ck_workflow_tag_events_delete_null_value",
        "workflow_tag_events",
        "event_type != 'delete' OR value IS NULL",
    )
    op.drop_index(
        "workflow_tag_events_org_value_active_idx",
        table_name="workflow_tag_events",
        postgresql_include=["workflow_permanent_id"],
        postgresql_where=sa.text("superseded_at IS NULL AND event_type = 'set'"),
    )
    op.drop_index(
        "workflow_tag_events_active_label_unique",
        table_name="workflow_tag_events",
        postgresql_where=sa.text("superseded_at IS NULL AND event_type = 'set' AND key IS NULL"),
    )
    op.drop_index(
        "workflow_tag_events_active_grouped_unique",
        table_name="workflow_tag_events",
        postgresql_where=sa.text("superseded_at IS NULL AND event_type = 'set' AND key IS NOT NULL"),
    )
    op.create_index(
        "workflow_tag_events_active_set_unique",
        "workflow_tag_events",
        ["organization_id", "workflow_permanent_id", "key"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL AND event_type = 'set'"),
    )
    op.alter_column("workflow_tag_events", "key", existing_type=sa.String(), nullable=False)
