"""add persistent session workflow binding

Revision ID: 3739fba3dc3d
Revises: 9e485c4e6177
Create Date: 2026-08-18T16:36:02.889554+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3739fba3dc3d"
down_revision: Union[str, None] = "9e485c4e6177"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def _binding_index_valid() -> bool | None:
    return (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT indisvalid "
                "FROM pg_catalog.pg_index "
                "WHERE indexrelid = to_regclass('uq_pbs_live_workflow_binding')"
            )
        )
        .scalar_one_or_none()
    )


def upgrade() -> None:
    op.execute("ALTER TABLE persistent_browser_sessions ADD COLUMN IF NOT EXISTS bound_workflow_permanent_id VARCHAR")
    op.execute("ALTER TABLE persistent_browser_sessions ADD COLUMN IF NOT EXISTS bound_key VARCHAR")
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = '3h';")
        try:
            if _binding_index_valid() is False:
                op.execute("DROP INDEX CONCURRENTLY uq_pbs_live_workflow_binding")
            op.execute(
                "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_pbs_live_workflow_binding "
                "ON persistent_browser_sessions "
                "(organization_id, bound_workflow_permanent_id, COALESCE(bound_key, '')) "
                "WHERE bound_workflow_permanent_id IS NOT NULL "
                "AND deleted_at IS NULL "
                "AND status IN ('created', 'running', 'retry')"
            )
            if _binding_index_valid() is not True:
                raise RuntimeError("uq_pbs_live_workflow_binding was not built as a valid index")
        finally:
            try:
                op.execute("RESET statement_timeout;")
            except Exception:
                pass


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS uq_pbs_live_workflow_binding")
    op.execute("ALTER TABLE persistent_browser_sessions DROP COLUMN IF EXISTS bound_key")
    op.execute("ALTER TABLE persistent_browser_sessions DROP COLUMN IF EXISTS bound_workflow_permanent_id")
