"""add credential folders

Revision ID: 8266bacba614
Revises: ed48b2c9cab2
Create Date: 2026-06-04T09:30:15.687201+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8266bacba614"
down_revision: Union[str, None] = "ed48b2c9cab2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "credential_folders",
        sa.Column("folder_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.organization_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("folder_id"),
    )
    op.create_index("credential_folder_organization_id_idx", "credential_folders", ["organization_id"], unique=False)
    op.create_index(
        "credential_folder_organization_title_idx",
        "credential_folders",
        ["organization_id", "title"],
        unique=False,
    )

    # ADD COLUMN is metadata-only on PG 11+ and the FK is added NOT VALID (no
    # full-table scan), so both take only a brief ACCESS EXCLUSIVE lock bounded
    # by lock_timeout. SET LOCAL auto-reverts at transaction end, so a rollback
    # can't leave the short timeout lingering on the connection.
    op.execute("SET LOCAL lock_timeout = '5s';")
    op.add_column("credentials", sa.Column("folder_id", sa.String(), nullable=True))
    op.create_foreign_key(
        "credentials_folder_id_fkey",
        "credentials",
        "credential_folders",
        ["folder_id"],
        ["folder_id"],
        ondelete="SET NULL",
        postgresql_not_valid=True,
    )

    # Validate outside the lock window — existing folder_id values are all NULL,
    # so this scan is trivial and takes only a SHARE UPDATE EXCLUSIVE lock.
    with op.get_context().autocommit_block():
        op.execute("ALTER TABLE credentials VALIDATE CONSTRAINT credentials_folder_id_fkey")

    # Build the folder_id lookup index without a long-held lock on credentials.
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = '1h';")
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS credential_folder_id_idx ON credentials (folder_id)")
        op.execute("RESET statement_timeout;")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = '1h';")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS credential_folder_id_idx")
        op.execute("RESET statement_timeout;")

    op.execute("SET LOCAL lock_timeout = '5s';")
    op.drop_constraint("credentials_folder_id_fkey", "credentials", type_="foreignkey")
    op.drop_column("credentials", "folder_id")

    op.drop_index("credential_folder_organization_title_idx", table_name="credential_folders")
    op.drop_index("credential_folder_organization_id_idx", table_name="credential_folders")
    op.drop_table("credential_folders")
