"""add TOTP external-message dedupe index

Revision ID: f65c97b21be8
Revises: 214808602675
Create Date: 2026-09-18T03:44:30.101783+00:00

"""

from typing import Sequence, Union

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f65c97b21be8"
down_revision: Union[str, None] = "214808602675"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "uq_totp_codes_org_external_message_id"


def upgrade() -> None:
    invalid_leftover = (
        op.get_bind()
        .execute(
            text(
                "SELECT c.oid::regclass::text FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
                "WHERE c.relname = :name AND i.indrelid = 'totp_codes'::regclass AND NOT i.indisvalid"
            ),
            {"name": INDEX_NAME},
        )
        .scalar()
    )
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s';")
        op.execute("SET statement_timeout = '3h';")
        try:
            if invalid_leftover:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {invalid_leftover};")
            op.execute(
                f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} "
                "ON totp_codes (organization_id, external_message_id) "
                "WHERE external_message_id IS NOT NULL"
            )
        finally:
            op.execute("RESET statement_timeout;")
            op.execute("RESET lock_timeout;")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s';")
        op.execute("SET statement_timeout = '3h';")
        try:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME};")
        finally:
            op.execute("RESET statement_timeout;")
            op.execute("RESET lock_timeout;")
