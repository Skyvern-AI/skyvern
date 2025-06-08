"""add_onepassword_login_credential_parameter_table

Revision ID: b2042a9668dd
Revises: babaa7307e8a
Create Date: 2025-06-08 20:23:03.782517+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import datetime # Added for default timestamps


# revision identifiers, used by Alembic.
revision: str = 'b2042a9668dd'
down_revision: Union[str, None] = 'babaa7307e8a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'onepassword_login_credential_parameters',
        sa.Column('onepassword_login_credential_parameter_id', sa.String(), nullable=False),
        sa.Column('workflow_id', sa.String(), nullable=False),
        sa.Column('key', sa.String(), nullable=False),
        sa.Column('description', sa.String(), nullable=True),
        sa.Column('onepassword_access_token_aws_secret_key', sa.String(), nullable=False),
        sa.Column('onepassword_item_id', sa.String(), nullable=False),
        sa.Column('onepassword_vault_id', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False, default=datetime.datetime.utcnow),
        sa.Column('modified_at', sa.DateTime(), nullable=False, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('onepassword_login_credential_parameter_id')
    )
    op.create_index(op.f('ix_onepassword_login_credential_parameters_onepassword_login_credential_parameter_id'), 'onepassword_login_credential_parameters', ['onepassword_login_credential_parameter_id'], unique=False)
    op.create_index(op.f('ix_onepassword_login_credential_parameters_workflow_id'), 'onepassword_login_credential_parameters', ['workflow_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_onepassword_login_credential_parameters_workflow_id'), table_name='onepassword_login_credential_parameters')
    op.drop_index(op.f('ix_onepassword_login_credential_parameters_onepassword_login_credential_parameter_id'), table_name='onepassword_login_credential_parameters')
    op.drop_table('onepassword_login_credential_parameters')
