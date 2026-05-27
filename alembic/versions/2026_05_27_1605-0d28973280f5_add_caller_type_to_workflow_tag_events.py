"""add caller_type to workflow_tag_events

Revision ID: 0d28973280f5
Revises: 4b254743ea85
Create Date: 2026-05-27T16:05:27.572803+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0d28973280f5"
down_revision: Union[str, None] = "4b254743ea85"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("workflow_tag_events", sa.Column("caller_type", sa.String(), nullable=True))
    # Values must stay in sync with CallerType (skyvern/forge/sdk/workflow/models/tags.py).
    # Adding a new CallerType variant requires a companion migration to widen this constraint.
    op.create_check_constraint(
        "ck_workflow_tag_events_caller_type",
        "workflow_tag_events",
        "caller_type IS NULL OR caller_type IN ('user', 'api_key', 'system')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_workflow_tag_events_caller_type", "workflow_tag_events", type_="check")
    op.drop_column("workflow_tag_events", "caller_type")
