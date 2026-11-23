"""Record which platform static module a script revision was pinned with.

Revision ID: d4a9e952c27f
Revises: a917f1c21903
Create Date: 2026-11-21T05:25:45.106022+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4a9e952c27f"
down_revision: Union[str, None] = "a917f1c21903"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("scripts", sa.Column("static_module", sa.String(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("scripts", "static_module")
