"""Create skyvern_project table

Revision ID: c21fff1a5fc5
Revises: 1d0a10ae2a13
Create Date: 2025-07-30 19:31:00.000000+00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c21fff1a5fc5"
down_revision: Union[str, None] = "1d0a10ae2a13"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "skyvern_projects",
        sa.Column("skyvern_project_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("artifact_id", sa.String(), nullable=True),
        sa.Column("structure", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("skyvern_project_id"),
    )
    op.create_index(
        op.f("ix_skyvern_projects_skyvern_project_id"),
        "skyvern_projects",
        ["skyvern_project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_skyvern_projects_organization_id"),
        "skyvern_projects",
        ["organization_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_skyvern_projects_organization_id"), table_name="skyvern_projects")
    op.drop_index(op.f("ix_skyvern_projects_skyvern_project_id"), table_name="skyvern_projects")
    op.drop_table("skyvern_projects")
