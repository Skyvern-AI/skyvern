"""add slug to organizations

Revision ID: 613e4f756671
Revises: 2c948904bb50
Create Date: 2026-08-21T22:29:43.807483+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "613e4f756671"
down_revision: Union[str, None] = "2c948904bb50"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("organizations", sa.Column("slug", sa.String(), nullable=True))
    op.create_index(
        "uq_organizations_slug",
        "organizations",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("slug IS NOT NULL"),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("uq_organizations_slug", table_name="organizations")
    op.drop_column("organizations", "slug")
