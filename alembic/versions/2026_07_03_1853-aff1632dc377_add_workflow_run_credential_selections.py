"""add workflow run credential selections

Revision ID: aff1632dc377
Revises: 2ac47bc1c075
Create Date: 2026-07-03T18:53:23.324057+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "aff1632dc377"
down_revision: Union[str, None] = "2ac47bc1c075"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("credential_parameters", sa.Column("credential_ids", sa.JSON(), nullable=True))
    op.add_column("credential_parameters", sa.Column("selection_strategy", sa.String(), nullable=True))
    op.create_table(
        "workflow_run_credential_selections",
        sa.Column("selection_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("workflow_run_id", sa.String(), nullable=False),
        sa.Column("workflow_permanent_id", sa.String(), nullable=False),
        sa.Column("parameter_key", sa.String(), nullable=False),
        sa.Column("credential_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("selection_id"),
        sa.UniqueConstraint("workflow_run_id", "parameter_key", name="uq_wrcs_workflow_run_parameter_key"),
    )
    op.create_index(
        "idx_wrcs_lru_lookup",
        "workflow_run_credential_selections",
        ["organization_id", "workflow_permanent_id", "parameter_key", "credential_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_wrcs_lru_lookup", table_name="workflow_run_credential_selections")
    op.drop_table("workflow_run_credential_selections")
    op.drop_column("credential_parameters", "selection_strategy")
    op.drop_column("credential_parameters", "credential_ids")
