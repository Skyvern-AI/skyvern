"""add workflow_definition_sha256 to workflow_runs

Revision ID: 798a79449b1e
Revises: 2b0a571b6e5e
Create Date: 2026-10-04T06:35:37.147784+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "798a79449b1e"
down_revision: Union[str, None] = "2b0a571b6e5e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("workflow_runs", sa.Column("workflow_definition_sha256", sa.String(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workflow_runs", "workflow_definition_sha256")
