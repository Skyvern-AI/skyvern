"""Add the remaining strict run-bound OTP claim schema.

Revision ID: 934a74685f8a
Revises: 13f3924eae2f
Create Date: 2026-11-23T05:13:20.156017+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "934a74685f8a"
down_revision: Union[str, None] = "13f3924eae2f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "otp_request_bindings",
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("otp_request_id", sa.String(), nullable=False),
        sa.Column("owner_type", sa.String(), nullable=False),
        sa.Column("owner_task_id", sa.String(), nullable=True),
        sa.Column("owner_workflow_run_id", sa.String(), nullable=True),
        sa.Column("owner_store", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "(owner_type = 'task' AND owner_task_id IS NOT NULL AND owner_workflow_run_id IS NULL) OR "
            "(owner_type = 'workflow_run' AND owner_task_id IS NULL AND owner_workflow_run_id IS NOT NULL)",
            name="ck_otp_request_bindings_single_owner",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.organization_id"]),
        sa.ForeignKeyConstraint(["owner_task_id"], ["tasks.task_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_workflow_run_id"], ["workflow_runs.workflow_run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("organization_id", "otp_request_id"),
    )
    op.create_table(
        "otp_request_acquisitions",
        sa.Column("acquisition_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("otp_request_id", sa.String(), nullable=False),
        sa.Column("execution_owner_type", sa.String(), nullable=False),
        sa.Column("execution_owner_id", sa.String(), nullable=False),
        sa.Column("execution_attempt", sa.String(), nullable=False),
        sa.Column("browser_type", sa.String(), nullable=True),
        sa.Column("browser_engine", sa.String(), nullable=True),
        sa.Column("worker_pid", sa.Integer(), nullable=False),
        sa.Column("worker_process_started_at", sa.String(), nullable=False),
        sa.Column("pod_uid", sa.String(), nullable=True),
        sa.Column("container_identity", sa.String(), nullable=True),
        sa.Column("lifecycle_state", sa.String(), server_default="sealed", nullable=False),
        sa.Column("acquisition_sealed_at", sa.DateTime(), nullable=False),
        sa.Column("stop_started_at", sa.DateTime(), nullable=True),
        sa.Column("stop_confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("failure_reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "otp_request_id"],
            ["otp_request_bindings.organization_id", "otp_request_bindings.otp_request_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("acquisition_id"),
        sa.UniqueConstraint(
            "organization_id",
            "otp_request_id",
            "execution_owner_type",
            "execution_owner_id",
            "execution_attempt",
            name="uq_otp_request_acquisitions_attempt",
        ),
    )
    op.add_column("tasks", sa.Column("otp_request_id", sa.String(), nullable=True))
    op.add_column("observer_cruises", sa.Column("otp_request_id", sa.String(), nullable=True))
    op.add_column("totp_codes", sa.Column("otp_request_id", sa.String(), nullable=True))
    op.add_column("totp_codes", sa.Column("claimed_by_task_id", sa.String(), nullable=True))
    op.add_column("totp_codes", sa.Column("claimed_by_workflow_run_id", sa.String(), nullable=True))
    op.add_column("totp_codes", sa.Column("claimed_at", sa.DateTime(), nullable=True))
    op.add_column("totp_codes", sa.Column("rejected_at", sa.DateTime(), nullable=True))
    op.alter_column("totp_codes", "totp_identifier", existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("otp_request_acquisitions")
    op.drop_table("otp_request_bindings")
    op.execute(
        "DELETE FROM totp_codes WHERE otp_request_id IS NOT NULL "
        "AND totp_identifier IS NULL AND source = 'verification_url'"
    )
    op.alter_column("totp_codes", "totp_identifier", existing_type=sa.String(), nullable=False)
    op.drop_column("totp_codes", "rejected_at")
    op.drop_column("totp_codes", "claimed_at")
    op.drop_column("totp_codes", "claimed_by_workflow_run_id")
    op.drop_column("totp_codes", "claimed_by_task_id")
    op.drop_column("totp_codes", "otp_request_id")
    op.drop_column("observer_cruises", "otp_request_id")
    op.drop_column("tasks", "otp_request_id")
