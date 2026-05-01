"""add source_browser_type to browser_profiles

Revision ID: 696a7bba1ce6
Revises: a5fe08ea4990
Create Date: 2026-04-30T09:04:59.788467+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "696a7bba1ce6"
down_revision: Union[str, None] = "a5fe08ea4990"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "browser_profiles",
        sa.Column("source_browser_type", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("browser_profiles", "source_browser_type")
