"""Index retry browser profiles and credential bindings for run search.

Revision ID: 13139662a50b
Revises: 55f527c1d158
Create Date: 2026-10-02T07:17:05.750400+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "13139662a50b"
down_revision: Union[str, None] = "55f527c1d158"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PROFILE_INDEX = "ix_workflow_run_attempts_profile_run_lookup"
_CREDENTIAL_INDEX = "ix_credential_parameters_credential_workflow_lookup"


def _drop_invalid_index(index_name: str) -> None:
    # A cancelled CONCURRENTLY build leaves an INVALID index that IF NOT EXISTS would stamp over.
    invalid = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
                "WHERE c.relname = :name AND NOT i.indisvalid"
            ),
            {"name": index_name},
        )
        .scalar()
    )
    if invalid:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}")


def upgrade() -> None:
    op.execute("ALTER TABLE workflow_run_attempts ADD COLUMN IF NOT EXISTS browser_profile_id VARCHAR")
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = '3h'")
        _drop_invalid_index(_PROFILE_INDEX)
        op.create_index(
            _PROFILE_INDEX,
            "workflow_run_attempts",
            ["browser_profile_id", "workflow_run_id"],
            postgresql_concurrently=True,
            postgresql_where=sa.text("browser_profile_id IS NOT NULL"),
            if_not_exists=True,
        )
        _drop_invalid_index(_CREDENTIAL_INDEX)
        op.create_index(
            _CREDENTIAL_INDEX,
            "credential_parameters",
            ["credential_id", "workflow_id"],
            postgresql_concurrently=True,
            if_not_exists=True,
        )
        op.execute("RESET statement_timeout")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            _CREDENTIAL_INDEX, table_name="credential_parameters", postgresql_concurrently=True, if_exists=True
        )
        op.drop_index(_PROFILE_INDEX, table_name="workflow_run_attempts", postgresql_concurrently=True, if_exists=True)
    op.drop_column("workflow_run_attempts", "browser_profile_id")
