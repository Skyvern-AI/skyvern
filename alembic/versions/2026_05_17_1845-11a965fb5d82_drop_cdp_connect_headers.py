"""drop cdp connect headers

Revision ID: 11a965fb5d82
Revises: 5f28f1f478d5
Create Date: 2026-05-17 18:45:00.000000+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "11a965fb5d82"
down_revision: Union[str, None] = "5f28f1f478d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("workflows", "cdp_connect_headers")
    op.drop_column("workflow_runs", "cdp_connect_headers")
    op.drop_column("tasks", "cdp_connect_headers")
    op.drop_column("observer_cruises", "cdp_connect_headers")


def downgrade() -> None:
    op.add_column("observer_cruises", sa.Column("cdp_connect_headers", sa.JSON(), nullable=True))
    op.add_column("tasks", sa.Column("cdp_connect_headers", sa.JSON(), nullable=True))
    op.add_column("workflow_runs", sa.Column("cdp_connect_headers", sa.JSON(), nullable=True))
    op.add_column("workflows", sa.Column("cdp_connect_headers", sa.JSON(), nullable=True))
