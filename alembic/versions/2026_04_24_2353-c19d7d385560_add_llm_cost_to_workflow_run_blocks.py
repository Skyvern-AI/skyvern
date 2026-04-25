"""add llm_cost to workflow_run_blocks

Revision ID: c19d7d385560
Revises: c9005bafa5ec
Create Date: 2026-04-24T23:53:46.912017+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c19d7d385560"
down_revision: Union[str, None] = "c9005bafa5ec"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workflow_run_blocks",
        sa.Column("llm_cost", sa.Numeric(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("workflow_run_blocks", "llm_cost")
