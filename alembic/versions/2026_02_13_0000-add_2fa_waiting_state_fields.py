"""add 2fa waiting state fields to workflow_runs and tasks

Revision ID: a1b2c3d4e5f6
Revises: 43217e31df12
Create Date: 2026-02-13 00:00:00.000000+00:00

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "43217e31df12"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS waiting_for_verification_code BOOLEAN NOT NULL DEFAULT false"
    )
    op.execute("ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS verification_code_identifier VARCHAR")
    op.execute("ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS verification_code_polling_started_at TIMESTAMP")
    op.execute(
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS waiting_for_verification_code BOOLEAN NOT NULL DEFAULT false"
    )
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS verification_code_identifier VARCHAR")
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS verification_code_polling_started_at TIMESTAMP")


def downgrade() -> None:
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS verification_code_polling_started_at")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS verification_code_identifier")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS waiting_for_verification_code")
    op.execute("ALTER TABLE workflow_runs DROP COLUMN IF EXISTS verification_code_polling_started_at")
    op.execute("ALTER TABLE workflow_runs DROP COLUMN IF EXISTS verification_code_identifier")
    op.execute("ALTER TABLE workflow_runs DROP COLUMN IF EXISTS waiting_for_verification_code")
