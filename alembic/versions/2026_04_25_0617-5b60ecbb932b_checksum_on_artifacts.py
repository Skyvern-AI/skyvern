"""checksum on artifacts

Revision ID: 5b60ecbb932b
Revises: d1474f2d1581
Create Date: 2026-04-25T06:17:32.060547+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5b60ecbb932b"
down_revision: Union[str, None] = "d1474f2d1581"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("artifacts", sa.Column("checksum", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("artifacts", "checksum")
