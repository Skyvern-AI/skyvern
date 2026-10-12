"""Store a capped copy of oversized run output values for run-detail reads.

Revision ID: 7a266f95f086
Revises: 4bb7e94abfb6
Create Date: 2026-10-12T23:49:57.872255+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7a266f95f086"
down_revision: Union[str, None] = "4bb7e94abfb6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LOCK_TIMEOUT = "5s"


def upgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
    # Nullable ADD COLUMN without a default is metadata-only on PG 11+, so neither table is
    # rewritten. Existing rows keep NULL and stay on the trim-on-read path.
    op.add_column("workflow_run_output_parameters", sa.Column("capped_value", sa.JSON(), nullable=True))
    op.add_column("workflow_run_output_parameters", sa.Column("capped_extracted_information", sa.JSON(), nullable=True))
    op.add_column("workflow_run_blocks", sa.Column("capped_output", sa.JSON(), nullable=True))

    op.execute(
        """
        CREATE FUNCTION clear_capped_workflow_run_output_parameter_on_scrub() RETURNS trigger AS $$
        BEGIN
            IF NEW.value IS NULL OR NEW.value::text = 'null' THEN
                NEW.capped_value := NULL;
                NEW.capped_extracted_information := NULL;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER clear_capped_workflow_run_output_parameter_on_scrub
        BEFORE UPDATE OF value ON workflow_run_output_parameters
        FOR EACH ROW EXECUTE FUNCTION clear_capped_workflow_run_output_parameter_on_scrub()
        """
    )
    op.execute(
        """
        CREATE FUNCTION clear_capped_workflow_run_block_on_scrub() RETURNS trigger AS $$
        BEGIN
            IF NEW.output IS NULL OR NEW.output::text = 'null' THEN
                NEW.capped_output := NULL;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER clear_capped_workflow_run_block_on_scrub
        BEFORE UPDATE OF output ON workflow_run_blocks
        FOR EACH ROW EXECUTE FUNCTION clear_capped_workflow_run_block_on_scrub()
        """
    )


def downgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
    op.execute("DROP TRIGGER clear_capped_workflow_run_block_on_scrub ON workflow_run_blocks")
    op.execute("DROP FUNCTION clear_capped_workflow_run_block_on_scrub()")
    op.execute("DROP TRIGGER clear_capped_workflow_run_output_parameter_on_scrub ON workflow_run_output_parameters")
    op.execute("DROP FUNCTION clear_capped_workflow_run_output_parameter_on_scrub()")
    op.drop_column("workflow_run_blocks", "capped_output")
    op.drop_column("workflow_run_output_parameters", "capped_extracted_information")
    op.drop_column("workflow_run_output_parameters", "capped_value")
