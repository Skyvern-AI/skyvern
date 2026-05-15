"""add cdp connect headers

Revision ID: 5f28f1f478d5
Revises: 43e7421f40f8
Create Date: 2026-05-15 16:16:44.776674+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5f28f1f478d5"
down_revision: Union[str, None] = "43e7421f40f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("observer_cruises", sa.Column("cdp_connect_headers", sa.JSON(), nullable=True))
    op.add_column("tasks", sa.Column("cdp_connect_headers", sa.JSON(), nullable=True))
    op.add_column("workflow_runs", sa.Column("cdp_connect_headers", sa.JSON(), nullable=True))
    op.add_column("workflows", sa.Column("cdp_connect_headers", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("workflows", "cdp_connect_headers")
    op.drop_column("workflow_runs", "cdp_connect_headers")
    op.drop_column("tasks", "cdp_connect_headers")
    op.drop_column("observer_cruises", "cdp_connect_headers")
