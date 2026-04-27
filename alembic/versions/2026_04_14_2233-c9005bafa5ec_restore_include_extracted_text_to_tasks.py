"""restore include_extracted_text to tasks

Revision ID: c9005bafa5ec
Revises: 12f6731887f4
Create Date: 2026-04-14T22:33:36.939859+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9005bafa5ec"
down_revision: Union[str, None] = "12f6731887f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("include_extracted_text", sa.Boolean(), server_default=sa.true(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("tasks", "include_extracted_text")
