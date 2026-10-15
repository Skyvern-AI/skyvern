"""add safe JSON to JSONB conversion helper

Revision ID: 18dfeaaa6771
Revises: 7a266f95f086
Create Date: 2026-10-12T23:49:57.873875+00:00

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "18dfeaaa6771"
down_revision: Union[str, None] = "7a266f95f086"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SAFE_JSONB_CAST_FUNCTION = """
CREATE OR REPLACE FUNCTION public.skyvern_json_to_jsonb_or_null(payload json)
RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
STRICT
SET search_path = pg_catalog
AS $function$
BEGIN
    RETURN payload::jsonb;
EXCEPTION WHEN data_exception THEN
    RETURN NULL;
END;
$function$;
"""


def upgrade() -> None:
    op.execute(_SAFE_JSONB_CAST_FUNCTION)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.skyvern_json_to_jsonb_or_null(json)")
