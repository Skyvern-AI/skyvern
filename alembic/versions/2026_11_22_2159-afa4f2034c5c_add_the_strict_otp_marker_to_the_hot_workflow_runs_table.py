"""Add the strict OTP marker to the hot workflow-runs table.

Revision ID: afa4f2034c5c
Revises: d4a9e952c27f
Create Date: 2026-11-22T21:59:08.494010+00:00

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "afa4f2034c5c"
down_revision: Union[str, None] = "d4a9e952c27f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        try:
            op.execute('ALTER TABLE "workflow_runs" ADD COLUMN IF NOT EXISTS otp_request_id VARCHAR')
        finally:
            op.execute("RESET lock_timeout")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        try:
            op.execute('ALTER TABLE "workflow_runs" DROP COLUMN IF EXISTS otp_request_id')
        finally:
            op.execute("RESET lock_timeout")
