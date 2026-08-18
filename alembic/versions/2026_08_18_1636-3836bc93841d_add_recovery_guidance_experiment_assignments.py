"""add recovery guidance experiment assignments

Revision ID: 3836bc93841d
Revises: 22705e03c606
Create Date: 2026-08-18T16:36:02.888171+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3836bc93841d"
down_revision: Union[str, None] = "22705e03c606"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "recovery_guidance_experiment_assignments",
        sa.Column("experiment_version", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("eligible_run_id", sa.String(), nullable=False),
        sa.Column("eligible_at", sa.DateTime(), nullable=False),
        sa.Column("arm", sa.String(), nullable=False),
        sa.Column("outcome_run_id", sa.String(), nullable=True),
        sa.Column("outcome_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("arm IN ('control', 'treatment')", name="ck_rg_assignments_arm"),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.organization_id"],
            name="fk_rg_assignments_organization_id",
        ),
        sa.ForeignKeyConstraint(
            ["eligible_run_id"],
            ["workflow_runs.workflow_run_id"],
            name="fk_rg_assignments_eligible_run_id",
        ),
        sa.ForeignKeyConstraint(
            ["outcome_run_id"],
            ["workflow_runs.workflow_run_id"],
            name="fk_rg_assignments_outcome_run_id",
        ),
        sa.PrimaryKeyConstraint(
            "experiment_version",
            "eligible_run_id",
            name="pk_rg_assignments",
        ),
        sa.UniqueConstraint(
            "experiment_version",
            "organization_id",
            name="uq_rg_assignments_experiment_organization",
        ),
    )
    op.create_index(
        "ix_rg_assignments_organization_experiment",
        "recovery_guidance_experiment_assignments",
        ["organization_id", "experiment_version"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_rg_assignments_organization_experiment",
        table_name="recovery_guidance_experiment_assignments",
    )
    op.drop_table("recovery_guidance_experiment_assignments")
