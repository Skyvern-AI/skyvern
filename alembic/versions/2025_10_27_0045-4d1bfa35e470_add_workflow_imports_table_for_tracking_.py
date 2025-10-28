"""Add workflow_imports table for tracking import progress

Revision ID: 4d1bfa35e470
Revises: add_folders_table
Create Date: 2025-10-27 00:45:32.487730+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4d1bfa35e470"
down_revision: Union[str, None] = "add_folders_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create workflow_imports table for tracking import progress
    op.create_table(
        "workflow_imports",
        sa.Column("import_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("file_name", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("workflow_id", sa.String(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("import_id"),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.organization_id"],
        ),
    )

    # Create indexes for efficient querying
    op.create_index(
        "workflow_import_organization_id_idx",
        "workflow_imports",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "workflow_import_status_idx",
        "workflow_imports",
        ["status"],
        unique=False,
    )
    op.create_index(
        "workflow_import_org_status_idx",
        "workflow_imports",
        ["organization_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    # Drop indexes
    op.drop_index("workflow_import_org_status_idx", table_name="workflow_imports")
    op.drop_index("workflow_import_status_idx", table_name="workflow_imports")
    op.drop_index("workflow_import_organization_id_idx", table_name="workflow_imports")

    # Drop table
    op.drop_table("workflow_imports")
