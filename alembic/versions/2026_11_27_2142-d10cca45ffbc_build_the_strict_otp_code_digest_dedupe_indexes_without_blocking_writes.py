"""Build the strict OTP code-digest dedupe indexes without blocking writes.

Revision ID: d10cca45ffbc
Revises: f51f65e1e496
Create Date: 2026-11-27T21:42:20.751772+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d10cca45ffbc"
down_revision: Union[str, None] = "f51f65e1e496"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX_DEFINITIONS = {
    "uq_totp_codes_strict_code_digest": (
        "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_totp_codes_strict_code_digest "
        "ON totp_codes (organization_id, otp_request_id, totp_identifier, code_digest) "
        "WHERE otp_request_id IS NOT NULL AND code_digest IS NOT NULL"
    ),
    "uq_totp_codes_strict_webhook_code_digest": (
        "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_totp_codes_strict_webhook_code_digest "
        "ON totp_codes (organization_id, otp_request_id, code_digest) "
        "WHERE otp_request_id IS NOT NULL AND totp_identifier IS NULL "
        "AND source = 'verification_url' AND code_digest IS NOT NULL"
    ),
}


def _invalid_index_exists(name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
                "WHERE c.relname = :name AND i.indrelid = 'totp_codes'::regclass AND NOT i.indisvalid"
            ),
            {"name": name},
        )
        .scalar()
    )


def upgrade() -> None:
    invalid_indexes = [name for name in _INDEX_DEFINITIONS if _invalid_index_exists(name)]
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = '3h'")
        try:
            for name in invalid_indexes:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            for definition in _INDEX_DEFINITIONS.values():
                op.execute(definition)
        finally:
            op.execute("RESET statement_timeout")
            op.execute("RESET lock_timeout")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = '3h'")
        try:
            for name in reversed(_INDEX_DEFINITIONS):
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        finally:
            op.execute("RESET statement_timeout")
            op.execute("RESET lock_timeout")
