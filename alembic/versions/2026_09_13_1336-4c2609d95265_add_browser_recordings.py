"""add browser recordings

Revision ID: 4c2609d95265
Revises: c55a3bc0f424
Create Date: 2026-09-13T13:36:32.280529+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4c2609d95265"
down_revision: Union[str, None] = "c55a3bc0f424"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "browser_recordings",
        sa.Column("recording_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("recording_attempt_id", sa.String(), nullable=False),
        sa.Column("browser_session_id", sa.String(), nullable=False),
        sa.Column("workflow_permanent_id", sa.String(), nullable=False),
        sa.Column("workflow_id", sa.String(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("recording_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.organization_id"]),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.workflow_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("recording_id"),
        sa.UniqueConstraint("organization_id", "recording_attempt_id", name="uc_browser_recordings_org_attempt"),
        sa.UniqueConstraint("workflow_id", name="uc_browser_recordings_workflow_id"),
    )
    op.create_index(
        "ix_browser_recordings_org_wpid",
        "browser_recordings",
        ["organization_id", "workflow_permanent_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_browser_recordings_org_wpid", table_name="browser_recordings")
    op.drop_table("browser_recordings")
