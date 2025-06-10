"""create onepassword_credential_parameters table

Revision ID: d9ce23f5729c
Revises: babaa7307e8a
Create Date: 2025-06-09 03:25:00.000000+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d9ce23f5729c"
down_revision: Union[str, None] = "babaa7307e8a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "onepassword_credential_parameters",
        sa.Column("onepassword_credential_parameter_id", sa.String(), nullable=False),
        sa.Column("workflow_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("secret_reference", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("onepassword_credential_parameter_id"),
    )
    op.create_index(
        op.f("ix_onepassword_credential_parameters_workflow_id"),
        "onepassword_credential_parameters",
        ["workflow_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_onepassword_credential_parameters_workflow_id"), table_name="onepassword_credential_parameters"
    )
    op.drop_table("onepassword_credential_parameters")
