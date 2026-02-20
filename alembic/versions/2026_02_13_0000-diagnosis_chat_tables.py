"""create diagnosis chat tables

Revision ID: diagnosis_chat_001
Revises: a1b2c3d4e5f6
Create Date: 2026-02-13 00:00:00.000000+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "diagnosis_chat_001"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create diagnosis_conversations table
    op.create_table(
        "diagnosis_conversations",
        sa.Column("diagnosis_conversation_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("workflow_run_id", sa.String(), nullable=False),
        sa.Column("escalation_ticket_id", sa.String(), nullable=True),
        sa.Column("escalation_ticket_url", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("summary", sa.UnicodeText(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("diagnosis_conversation_id"),
    )
    op.create_index(
        "idx_diagnosis_conversations_org_wfr",
        "diagnosis_conversations",
        ["organization_id", "workflow_run_id"],
        unique=False,
    )
    op.create_index(
        "idx_diagnosis_conversations_org_created",
        "diagnosis_conversations",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_diagnosis_conversations_workflow_run_id"),
        "diagnosis_conversations",
        ["workflow_run_id"],
        unique=False,
    )

    # Create diagnosis_messages table
    op.create_table(
        "diagnosis_messages",
        sa.Column("diagnosis_message_id", sa.String(), nullable=False),
        sa.Column("diagnosis_conversation_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.UnicodeText(), nullable=False),
        sa.Column("message_metadata", sa.JSON(), nullable=True),
        sa.Column("input_token_count", sa.Integer(), nullable=True),
        sa.Column("output_token_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("diagnosis_message_id"),
    )
    op.create_index(
        "idx_diagnosis_messages_conv_created",
        "diagnosis_messages",
        ["diagnosis_conversation_id", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_diagnosis_messages_diagnosis_conversation_id"),
        "diagnosis_messages",
        ["diagnosis_conversation_id"],
        unique=False,
    )


def downgrade() -> None:
    # Drop diagnosis_messages table and indexes
    op.drop_index(
        op.f("ix_diagnosis_messages_diagnosis_conversation_id"),
        table_name="diagnosis_messages",
    )
    op.drop_index("idx_diagnosis_messages_conv_created", table_name="diagnosis_messages")
    op.drop_table("diagnosis_messages")

    # Drop diagnosis_conversations table and indexes
    op.drop_index(
        op.f("ix_diagnosis_conversations_workflow_run_id"),
        table_name="diagnosis_conversations",
    )
    op.drop_index("idx_diagnosis_conversations_org_created", table_name="diagnosis_conversations")
    op.drop_index("idx_diagnosis_conversations_org_wfr", table_name="diagnosis_conversations")
    op.drop_table("diagnosis_conversations")
