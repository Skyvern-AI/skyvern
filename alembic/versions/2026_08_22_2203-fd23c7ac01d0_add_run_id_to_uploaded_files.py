"""add run_id to uploaded_files

Revision ID: fd23c7ac01d0
Revises: 613e4f756671
Create Date: 2026-08-22T22:03:10.974717+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "fd23c7ac01d0"
down_revision: Union[str, None] = "613e4f756671"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s';")
    op.add_column("uploaded_files", sa.Column("run_id", sa.String(), nullable=True))
    op.create_index(
        "ix_uploaded_files_run_id_live",
        "uploaded_files",
        ["run_id"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL AND run_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s';")
    op.drop_index("ix_uploaded_files_run_id_live", table_name="uploaded_files")
    op.drop_column("uploaded_files", "run_id")
