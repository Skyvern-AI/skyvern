"""browser profile name unique partial on deleted_at

Revision ID: e2d87251fee8
Revises: aff1632dc377
Create Date: 2026-07-07T15:35:51.042177+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2d87251fee8"
down_revision: Union[str, None] = "aff1632dc377"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index(
        "uq_browser_profiles_org_name_user",
        table_name="browser_profiles",
        postgresql_where=sa.text("is_managed = false"),
    )
    op.create_index(
        "uq_browser_profiles_org_name_user",
        "browser_profiles",
        ["organization_id", "name"],
        unique=True,
        postgresql_where=sa.text("is_managed = false AND deleted_at IS NULL"),
        sqlite_where=sa.text("is_managed = false AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_browser_profiles_org_name_user",
        table_name="browser_profiles",
        postgresql_where=sa.text("is_managed = false AND deleted_at IS NULL"),
    )
    op.create_index(
        "uq_browser_profiles_org_name_user",
        "browser_profiles",
        ["organization_id", "name"],
        unique=True,
        postgresql_where=sa.text("is_managed = false"),
        sqlite_where=sa.text("is_managed = false"),
    )
