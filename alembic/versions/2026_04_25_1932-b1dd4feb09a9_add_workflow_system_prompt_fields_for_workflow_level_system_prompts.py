"""add workflow_system_prompt fields for workflow-level system prompts

Revision ID: b1dd4feb09a9
Revises: d1474f2d1581
Create Date: 2026-04-25T19:32:07.728570+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1dd4feb09a9"
down_revision: Union[str, None] = "d1474f2d1581"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("observer_cruises", sa.Column("workflow_system_prompt", sa.UnicodeText(), nullable=True))
    op.add_column("tasks", sa.Column("workflow_system_prompt", sa.UnicodeText(), nullable=True))
    op.add_column(
        "workflow_runs",
        sa.Column(
            "ignore_inherited_workflow_system_prompt", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("workflow_runs", "ignore_inherited_workflow_system_prompt")
    op.drop_column("tasks", "workflow_system_prompt")
    op.drop_column("observer_cruises", "workflow_system_prompt")
