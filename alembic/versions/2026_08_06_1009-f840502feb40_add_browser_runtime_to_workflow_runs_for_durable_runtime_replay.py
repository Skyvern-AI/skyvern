"""Add browser_runtime to workflow_runs for durable runtime replay

Revision ID: f840502feb40
Revises: 1ee67c216fca
Create Date: 2026-08-06T10:09:38.416055+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f840502feb40"
down_revision: Union[str, None] = "1ee67c216fca"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "workflow_runs",
        sa.Column("browser_runtime", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workflow_runs", "browser_runtime")
