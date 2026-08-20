"""add org default llm keys

Revision ID: e1ffa32660cc
Revises: 70e4ff9019f1
Create Date: 2026-08-02T03:28:28.839833+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1ffa32660cc"
down_revision: Union[str, None] = "70e4ff9019f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("organizations", sa.Column("default_llm_key", sa.String(), nullable=True))
    op.add_column("organizations", sa.Column("default_secondary_llm_key", sa.String(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("organizations", "default_secondary_llm_key")
    op.drop_column("organizations", "default_llm_key")
