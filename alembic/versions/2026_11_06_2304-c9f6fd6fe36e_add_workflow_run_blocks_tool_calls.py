"""add workflow_run_blocks.tool_calls

Revision ID: c9f6fd6fe36e
Revises: 2868bd257141
Create Date: 2026-11-06T23:04:32.346189+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9f6fd6fe36e"
down_revision: Union[str, None] = "2868bd257141"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SCRUB_TRIGGER_FUNCTION = """
    CREATE OR REPLACE FUNCTION clear_capped_workflow_run_block_on_scrub() RETURNS trigger AS $$
    BEGIN
        IF NEW.output IS NULL OR NEW.output::text = 'null' THEN
            NEW.capped_output := NULL;{tool_calls}
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("workflow_run_blocks", sa.Column("tool_calls", sa.JSON(), nullable=True))
    op.execute(_SCRUB_TRIGGER_FUNCTION.format(tool_calls="\n            NEW.tool_calls := NULL;"))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(_SCRUB_TRIGGER_FUNCTION.format(tool_calls=""))
    op.drop_column("workflow_run_blocks", "tool_calls")
