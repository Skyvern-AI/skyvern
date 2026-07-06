"""managed browser profiles for save and reuse session

Revision ID: ecf365563e98
Revises: a1cad008154a
Create Date: 2026-07-02T23:17:33.611632+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ecf365563e98"
down_revision: Union[str, None] = "a1cad008154a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "browser_profiles",
        sa.Column("is_managed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("browser_profiles", sa.Column("workflow_permanent_id", sa.String(), nullable=True))
    op.add_column("browser_profiles", sa.Column("browser_profile_key_digest", sa.String(), nullable=True))
    op.create_index(
        "uq_browser_profiles_org_name_user",
        "browser_profiles",
        ["organization_id", "name"],
        unique=True,
        postgresql_where=sa.text("is_managed = false"),
        sqlite_where=sa.text("is_managed = false"),
    )
    op.create_index(
        "uq_browser_profiles_managed_segment",
        "browser_profiles",
        ["organization_id", "workflow_permanent_id", "browser_profile_key_digest"],
        unique=True,
        # Exclude soft-deleted rows so deleting a managed profile doesn't tombstone the
        # segment — the next run re-creates it instead of colliding on the unique key.
        postgresql_where=sa.text("is_managed = true AND deleted_at IS NULL"),
        sqlite_where=sa.text("is_managed = true AND deleted_at IS NULL"),
    )
    op.create_index("idx_browser_profiles_wpid", "browser_profiles", ["workflow_permanent_id"], unique=False)
    op.drop_constraint("uc_org_browser_profile_name", "browser_profiles", type_="unique")


def downgrade() -> None:
    op.execute("DELETE FROM browser_profiles WHERE is_managed = true")
    op.create_unique_constraint("uc_org_browser_profile_name", "browser_profiles", ["organization_id", "name"])
    op.drop_index("idx_browser_profiles_wpid", table_name="browser_profiles")
    op.drop_index(
        "uq_browser_profiles_managed_segment",
        table_name="browser_profiles",
        postgresql_where=sa.text("is_managed = true AND deleted_at IS NULL"),
    )
    op.drop_index(
        "uq_browser_profiles_org_name_user",
        table_name="browser_profiles",
        postgresql_where=sa.text("is_managed = false"),
    )
    op.drop_column("browser_profiles", "browser_profile_key_digest")
    op.drop_column("browser_profiles", "workflow_permanent_id")
    op.drop_column("browser_profiles", "is_managed")
