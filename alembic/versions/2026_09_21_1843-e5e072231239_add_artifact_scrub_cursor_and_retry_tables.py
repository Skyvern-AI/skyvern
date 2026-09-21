"""add artifact scrub cursor and retry tables

Revision ID: e5e072231239
Revises: 2dace0960323
Create Date: 2026-09-21T18:43:50.127237+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5e072231239"
down_revision: Union[str, None] = "2dace0960323"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "artifact_scrub_cursors",
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("cursor_created_at", sa.DateTime(), nullable=True),
        sa.Column("cursor_artifact_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.organization_id"]),
        sa.PrimaryKeyConstraint("organization_id"),
    )
    op.create_table(
        "artifact_scrub_retries",
        sa.Column("artifact_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("first_failed_at", sa.DateTime(), nullable=False),
        sa.Column("last_failed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.organization_id"]),
        sa.PrimaryKeyConstraint("artifact_id"),
    )
    op.create_index(
        "ix_artifact_scrub_retries_org_first_failed",
        "artifact_scrub_retries",
        ["organization_id", "first_failed_at", "artifact_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_artifact_scrub_retries_org_first_failed", table_name="artifact_scrub_retries")
    op.drop_table("artifact_scrub_retries")
    op.drop_table("artifact_scrub_cursors")
