"""add credential fallback fields

Revision ID: d9ab2c4d41e5
Revises: 1915b0e1126e
Create Date: 2026-07-17T17:52:48.243761+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d9ab2c4d41e5"
down_revision: Union[str, None] = "1915b0e1126e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("credential_parameters", sa.Column("fallback_credential_ids", sa.JSON(), nullable=True))
    op.add_column("credential_parameters", sa.Column("fallback_trigger", sa.String(), nullable=True))
    op.add_column("workflow_runs", sa.Column("retried_from_workflow_run_id", sa.String(), nullable=True))
    op.add_column("workflow_runs", sa.Column("fallback_attempt", sa.Integer(), nullable=True))

    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = '1h'")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_workflow_runs_retried_from_workflow_run_id")
        op.execute(
            """
            CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ix_workflow_runs_retried_from_workflow_run_id
            ON workflow_runs (retried_from_workflow_run_id)
            WHERE retried_from_workflow_run_id IS NOT NULL
            """
        )
        op.execute("RESET statement_timeout")
        op.execute("RESET lock_timeout")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = '1h'")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_workflow_runs_retried_from_workflow_run_id")
        op.execute("RESET statement_timeout")
        op.execute("RESET lock_timeout")

    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workflow_runs", "fallback_attempt")
    op.drop_column("workflow_runs", "retried_from_workflow_run_id")
    op.drop_column("credential_parameters", "fallback_trigger")
    op.drop_column("credential_parameters", "fallback_credential_ids")
