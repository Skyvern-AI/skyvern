"""Build strict OTP claim indexes without blocking writes.

Revision ID: 738dfed1bb9b
Revises: 934a74685f8a
Create Date: 2026-11-23T05:13:20.160050+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "738dfed1bb9b"
down_revision: Union[str, None] = "934a74685f8a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TEXT_DIGEST_FUNCTION = """
CREATE OR REPLACE FUNCTION public.skyvern_sha256_text_utf8(value text)
RETURNS bytea
LANGUAGE sql
IMMUTABLE
STRICT
PARALLEL SAFE
SECURITY INVOKER
SET search_path = pg_catalog
AS $function$
    SELECT pg_catalog.sha256(pg_catalog.convert_to(value, 'UTF8'))
$function$
"""
_INDEX_DEFINITIONS = {
    "ix_totp_codes_strict_claim": (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_totp_codes_strict_claim "
        "ON totp_codes (organization_id, otp_request_id, totp_identifier, created_at) "
        "WHERE otp_request_id IS NOT NULL AND claimed_at IS NULL"
    ),
    "uq_totp_codes_strict_code": (
        "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_totp_codes_strict_code "
        "ON totp_codes (organization_id, otp_request_id, totp_identifier, "
        "(public.skyvern_sha256_text_utf8(code))) "
        "WHERE otp_request_id IS NOT NULL AND code IS NOT NULL"
    ),
    "uq_totp_codes_strict_webhook_code": (
        "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_totp_codes_strict_webhook_code "
        "ON totp_codes (organization_id, otp_request_id, (public.skyvern_sha256_text_utf8(code))) "
        "WHERE otp_request_id IS NOT NULL AND totp_identifier IS NULL "
        "AND source = 'verification_url' AND code IS NOT NULL"
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
            op.execute(_TEXT_DIGEST_FUNCTION)
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
            op.execute("DROP FUNCTION IF EXISTS public.skyvern_sha256_text_utf8(text)")
        finally:
            op.execute("RESET statement_timeout")
            op.execute("RESET lock_timeout")
