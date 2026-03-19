"""add error_codes to workflow_run_blocks

Revision ID: 786acdf95243
Revises: e8c78980ed9d
Create Date: 2026-03-19 07:35:01.666339+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "786acdf95243"
down_revision: Union[str, None] = "e8c78980ed9d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("workflow_run_blocks", sa.Column("error_codes", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("workflow_run_blocks", "error_codes")
