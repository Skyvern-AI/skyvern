"""add max_steps_per_workflow_run to organizations

Revision ID: 2704b02dede9
Revises: 53a306b96ed7
Create Date: 2026-05-06T19:30:35.812486+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2704b02dede9"
down_revision: Union[str, None] = "53a306b96ed7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("max_steps_per_workflow_run", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("organizations", "max_steps_per_workflow_run")
