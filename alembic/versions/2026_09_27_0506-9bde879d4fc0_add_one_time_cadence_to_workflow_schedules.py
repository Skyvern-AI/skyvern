"""add one-time cadence to workflow_schedules

Revision ID: 9bde879d4fc0
Revises: adf6f1f2e672
Create Date: 2026-09-27T05:06:02.153506+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9bde879d4fc0"
down_revision: Union[str, None] = "adf6f1f2e672"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CADENCE_CHECK = "ck_workflow_schedules_one_cadence"
OLD_CADENCE_RULE = (
    "(cron_expression IS NULL) <> (interval_seconds IS NULL) AND (interval_seconds IS NULL) = (first_fire_at IS NULL)"
)
NEW_CADENCE_RULE = (
    "(CASE WHEN cron_expression IS NULL THEN 0 ELSE 1 END + CASE WHEN interval_seconds IS NULL THEN 0 ELSE 1 END "
    "+ CASE WHEN run_at IS NULL THEN 0 ELSE 1 END) = 1 "
    "AND (interval_seconds IS NULL) = (first_fire_at IS NULL) "
    "AND (run_at IS NULL) = (dispatch_status IS NULL)"
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("workflow_schedules", sa.Column("run_at", sa.DateTime(), nullable=True))
    op.add_column("workflow_schedules", sa.Column("dispatch_status", sa.String(), nullable=True))
    op.add_column("workflow_schedules", sa.Column("workflow_run_id", sa.String(), nullable=True))
    op.drop_constraint(CADENCE_CHECK, "workflow_schedules", type_="check")
    op.create_check_constraint(CADENCE_CHECK, "workflow_schedules", NEW_CADENCE_RULE)


def downgrade() -> None:
    count = (
        op.get_bind().execute(sa.text("SELECT COUNT(*) FROM workflow_schedules WHERE run_at IS NOT NULL")).scalar_one()
    )
    if count:
        raise RuntimeError(
            f"Refusing to downgrade: workflow_schedules holds {count} one-time schedules. Cancel or delete the live "
            "ones through the schedules API, then run `DELETE FROM workflow_schedules WHERE run_at IS NOT NULL` "
            "and retry."
        )
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint(CADENCE_CHECK, "workflow_schedules", type_="check")
    op.create_check_constraint(CADENCE_CHECK, "workflow_schedules", OLD_CADENCE_RULE)
    op.drop_column("workflow_schedules", "workflow_run_id")
    op.drop_column("workflow_schedules", "dispatch_status")
    op.drop_column("workflow_schedules", "run_at")
