"""add browser type to workflows and workflow runs

Revision ID: de8067e9e60c
Revises: f57aeaae936b
Create Date: 2026-09-15T09:50:10.055078+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "de8067e9e60c"
down_revision: Union[str, None] = "f57aeaae936b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("workflow_runs", sa.Column("browser_type", sa.String(), nullable=True))
    op.add_column("workflows", sa.Column("browser_type", sa.String(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workflow_runs", "browser_type")
    op.drop_column("workflows", "browser_type")
