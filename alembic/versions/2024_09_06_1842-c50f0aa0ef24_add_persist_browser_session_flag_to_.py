"""Add persist_browser_session flag to workflows

Revision ID: c50f0aa0ef24
Revises: 0de9150bc624
Create Date: 2024-09-06 18:42:42.677573+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c50f0aa0ef24"
down_revision: Union[str, None] = "0de9150bc624"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column("workflows", sa.Column("persist_browser_session", sa.Boolean(), nullable=True))
    op.execute("UPDATE workflows SET persist_browser_session = False WHERE persist_browser_session IS NULL")
    op.alter_column("workflows", "persist_browser_session", nullable=False)
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column("workflows", "persist_browser_session")
    # ### end Alembic commands ###
