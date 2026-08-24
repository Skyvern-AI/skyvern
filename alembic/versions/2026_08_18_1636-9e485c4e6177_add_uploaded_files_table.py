"""add uploaded_files table

Revision ID: 9e485c4e6177
Revises: f926d887da00
Create Date: 2026-08-18T16:36:02.889111+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9e485c4e6177"
down_revision: Union[str, None] = "f926d887da00"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s';")
    op.create_table(
        "uploaded_files",
        sa.Column("file_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("storage_uri", sa.String(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("size_bytes >= 0", name="ck_uploaded_files_size_bytes_non_negative"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.organization_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("file_id"),
    )
    op.create_index("ix_uploaded_files_organization_id", "uploaded_files", ["organization_id"], unique=False)
    op.create_index(
        "ux_uploaded_files_org_storage_uri_live",
        "uploaded_files",
        ["organization_id", "storage_uri"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_uploaded_files_expires_at_live",
        "uploaded_files",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL AND expires_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s';")
    op.drop_index("ix_uploaded_files_expires_at_live", table_name="uploaded_files")
    op.drop_index("ux_uploaded_files_org_storage_uri_live", table_name="uploaded_files")
    op.drop_index("ix_uploaded_files_organization_id", table_name="uploaded_files")
    op.drop_table("uploaded_files")
