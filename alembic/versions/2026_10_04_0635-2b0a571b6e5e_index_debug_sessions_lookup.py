"""index debug_sessions lookup

Revision ID: 2b0a571b6e5e
Revises: 6fbee6664b86
Create Date: 2026-10-04T06:35:37.146186+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2b0a571b6e5e"
down_revision: Union[str, None] = "6fbee6664b86"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "ix_debug_sessions_org_wpid_user_created_at"
_TABLE = "debug_sessions"


def _index_is_valid() -> bool | None:
    return (
        op.get_bind()
        .execute(
            sa.text("SELECT indisvalid FROM pg_catalog.pg_index WHERE indexrelid = to_regclass(:name)"),
            {"name": INDEX_NAME},
        )
        .scalar_one_or_none()
    )


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = '10min';")
        # A failed CONCURRENTLY build leaves an INVALID index of this name, which IF NOT EXISTS would keep.
        # Assumes a single migration runner: another runner's in-flight build also reads as INVALID.
        if _index_is_valid() is False:
            op.execute(f"DROP INDEX CONCURRENTLY {INDEX_NAME};")
        # Deliberately not partial on status: the lookup binds status as a parameter, so a generic plan
        # cannot prove `status = 'created'` and would skip a partial index.
        op.create_index(
            INDEX_NAME,
            _TABLE,
            ["organization_id", "workflow_permanent_id", "user_id", "created_at"],
            unique=False,
            postgresql_concurrently=True,
            if_not_exists=True,
        )
        op.execute("RESET statement_timeout;")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = '10min';")
        op.drop_index(
            INDEX_NAME,
            table_name=_TABLE,
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.execute("RESET statement_timeout;")
