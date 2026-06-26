"""add workflow_copilot_completion_criteria_sets table

Revision ID: 218048b68412
Revises: a8c4e2f9d6b1
Create Date: 2026-06-14T22:21:36.243898+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "218048b68412"
down_revision: Union[str, None] = "a8c4e2f9d6b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "workflow_copilot_completion_criteria_sets"


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if inspector.has_table(_TABLE):
        return

    if conn.dialect.name == "postgresql":
        op.execute(sa.text("SET lock_timeout = '2s'"))

    op.create_table(
        _TABLE,
        sa.Column("completion_criteria_set_id", sa.String(), nullable=False),
        sa.Column("organization_id", sa.String(), nullable=False),
        sa.Column("workflow_copilot_chat_id", sa.String(), nullable=False),
        sa.Column("goal_epoch", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("criteria", sa.JSON(), nullable=False),
        sa.Column("source_turn_id", sa.String(), nullable=True),
        sa.Column("source_goal_text", sa.UnicodeText(), nullable=True),
        sa.Column("consecutive_all_no_evidence", sa.Integer(), nullable=False),
        sa.Column("tripwire_fired", sa.Boolean(), nullable=False),
        sa.Column("last_fully_satisfied_workflow_yaml", sa.UnicodeText(), nullable=True),
        sa.Column("superseded_by_set_id", sa.String(), nullable=True),
        sa.Column("superseded_at", sa.DateTime(), nullable=True),
        sa.Column("supersede_reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("completion_criteria_set_id"),
    )
    op.create_index("wcccs_org_chat_index", _TABLE, ["organization_id", "workflow_copilot_chat_id"])

    if conn.dialect.name == "postgresql":
        op.execute(sa.text("SET lock_timeout = '0'"))


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return
    op.drop_index("wcccs_org_chat_index", table_name=_TABLE)
    op.drop_table(_TABLE)
