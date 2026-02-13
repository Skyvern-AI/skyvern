"""add 2fa waiting state fields to workflow_runs

Revision ID: a1b2c3d4e5f6
Revises: 43217e31df12
Create Date: 2026-02-13 00:00:00.000000+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "43217e31df12"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add 2FA verification code waiting state fields to workflow_runs table
    op.add_column(
        "workflow_runs",
        sa.Column("waiting_for_verification_code", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "workflow_runs",
        sa.Column("verification_code_identifier", sa.String(), nullable=True),
    )
    op.add_column(
        "workflow_runs",
        sa.Column("verification_code_polling_started_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workflow_runs", "verification_code_polling_started_at")
    op.drop_column("workflow_runs", "verification_code_identifier")
    op.drop_column("workflow_runs", "waiting_for_verification_code")
