"""Add index for revoked email forwarding address lookups.

Revision ID: 77ff13cea42b
Revises: 626683df7bd7
Create Date: 2026-11-01T14:51:19.249122+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "77ff13cea42b"
down_revision: Union[str, None] = "626683df7bd7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "ix_organization_forwarding_addresses_revoked_email"
TABLE_NAME = "organization_forwarding_addresses"


def upgrade() -> None:
    invalid_leftover = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT c.oid::regclass::text FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
                f"WHERE c.relname = :name AND i.indrelid = '{TABLE_NAME}'::regclass AND NOT i.indisvalid"
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
            op.create_index(
                INDEX_NAME,
                TABLE_NAME,
                [sa.text("lower(email_address)")],
                postgresql_where=sa.text("status = 'revoked'"),
                postgresql_concurrently=True,
                if_not_exists=True,
            )
        finally:
            op.execute("RESET statement_timeout;")
            op.execute("RESET lock_timeout;")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s';")
        op.execute("SET statement_timeout = '3h';")
        try:
            op.drop_index(
                INDEX_NAME,
                table_name=TABLE_NAME,
                postgresql_concurrently=True,
                if_exists=True,
            )
        finally:
            op.execute("RESET statement_timeout;")
            op.execute("RESET lock_timeout;")
