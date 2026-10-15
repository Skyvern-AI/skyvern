"""catch oversized JSON in the safe JSONB conversion helper

Revision ID: 4452dbf24863
Revises: 18dfeaaa6771
Create Date: 2026-10-12T23:49:57.875047+00:00

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4452dbf24863"
down_revision: Union[str, None] = "18dfeaaa6771"
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
EXCEPTION WHEN data_exception OR program_limit_exceeded THEN
    RETURN NULL;
END;
$function$;
"""


def upgrade() -> None:
    op.execute(_SAFE_JSONB_CAST_FUNCTION)


def downgrade() -> None:
    pass
