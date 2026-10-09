"""Index job recipe runs for the recipe runs listing.

Revision ID: 4bb7e94abfb6
Revises: 7caf8d23277b
Create Date: 2026-10-08T18:56:05.827377+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4bb7e94abfb6"
down_revision: Union[str, None] = "7caf8d23277b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX_NAME = "ix_workflow_runs_job_recipe_listing"
_RECIPE_TRIGGER_TYPES = "('job_recipe_apply', 'job_recipe_extract')"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = '3h'")
        # A cancelled CONCURRENTLY build leaves an INVALID index that IF NOT EXISTS would stamp over.
        invalid = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
                    "WHERE c.relname = :name AND NOT i.indisvalid"
                ),
                {"name": _INDEX_NAME},
            )
            .scalar()
        )
        if invalid:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
        op.create_index(
            _INDEX_NAME,
            "workflow_runs",
            ["organization_id", sa.text("created_at DESC"), sa.text("workflow_run_id DESC")],
            postgresql_concurrently=True,
            postgresql_where=sa.text(f"trigger_type IN {_RECIPE_TRIGGER_TYPES}"),
            if_not_exists=True,
        )
        op.execute("RESET statement_timeout")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            _INDEX_NAME,
            table_name="workflow_runs",
            postgresql_concurrently=True,
            if_exists=True,
        )
