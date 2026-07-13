"""allow system source on workflow tag events

Revision ID: e4db575f75ee
Revises: a0d23605c574
Create Date: 2026-07-10T03:03:42.723810+00:00

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4db575f75ee"
down_revision: Union[str, None] = "a0d23605c574"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_workflow_tag_events_source", "workflow_tag_events", type_="check")
    op.create_check_constraint(
        "ck_workflow_tag_events_source",
        "workflow_tag_events",
        "source IN ('manual', 'bulk_apply', 'backfill', 'inherited', 'import', 'system')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_workflow_tag_events_source", "workflow_tag_events", type_="check")
    op.create_check_constraint(
        "ck_workflow_tag_events_source",
        "workflow_tag_events",
        "source IN ('manual', 'bulk_apply', 'backfill', 'inherited', 'import')",
    )
