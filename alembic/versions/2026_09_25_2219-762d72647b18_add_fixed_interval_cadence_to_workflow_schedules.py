"""add fixed-interval cadence to workflow_schedules

Revision ID: 762d72647b18
Revises: dc24a9eab0fb
Create Date: 2026-09-25T22:19:21.286896+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "762d72647b18"
down_revision: Union[str, None] = "dc24a9eab0fb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CADENCE_CHECK = "ck_workflow_schedules_one_cadence"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("workflow_schedules", sa.Column("interval_seconds", sa.Integer(), nullable=True))
    op.add_column("workflow_schedules", sa.Column("first_fire_at", sa.DateTime(), nullable=True))
    op.alter_column("workflow_schedules", "cron_expression", existing_type=sa.String(), nullable=True)
    op.create_check_constraint(
        CADENCE_CHECK,
        "workflow_schedules",
        "(cron_expression IS NULL) <> (interval_seconds IS NULL) "
        "AND (interval_seconds IS NULL) = (first_fire_at IS NULL)",
    )


def downgrade() -> None:
    live, soft_deleted = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT COUNT(*) FILTER (WHERE deleted_at IS NULL), COUNT(*) FILTER (WHERE deleted_at IS NOT NULL) "
                "FROM workflow_schedules WHERE interval_seconds IS NOT NULL"
            )
        )
        .one()
    )
    if live or soft_deleted:
        raise RuntimeError(
            f"Refusing to downgrade: workflow_schedules holds {live} live and {soft_deleted} soft-deleted interval "
            "schedules. Delete the live ones through the schedules API (which also removes their Temporal schedules), "
            "then run `DELETE FROM workflow_schedules WHERE interval_seconds IS NOT NULL AND deleted_at IS NOT NULL` "
            "and retry."
        )
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint(CADENCE_CHECK, "workflow_schedules", type_="check")
    op.alter_column("workflow_schedules", "cron_expression", existing_type=sa.String(), nullable=False)
    op.drop_column("workflow_schedules", "first_fire_at")
    op.drop_column("workflow_schedules", "interval_seconds")
