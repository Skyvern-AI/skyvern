"""add_copilot_attribution_columns

Revision ID: bd362c15b74b
Revises: 70b5f11e3655
Create Date: 2026-04-26T03:38:25.486705+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bd362c15b74b"
down_revision: Union[str, None] = "70b5f11e3655"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("workflows", sa.Column("created_by", sa.String(), nullable=True))
    op.add_column("workflows", sa.Column("edited_by", sa.String(), nullable=True))
    op.add_column("workflow_runs", sa.Column("copilot_session_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("workflow_runs", "copilot_session_id")
    op.drop_column("workflows", "edited_by")
    op.drop_column("workflows", "created_by")
