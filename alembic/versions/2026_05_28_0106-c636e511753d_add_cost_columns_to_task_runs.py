"""add_cost_columns_to_task_runs

Revision ID: c636e511753d
Revises: 0d28973280f5
Create Date: 2026-05-28T01:06:49.862871+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c636e511753d"
down_revision: Union[str, None] = "0d28973280f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("task_runs", sa.Column("llm_cost", sa.Numeric(), nullable=True))
    op.add_column("task_runs", sa.Column("proxy_cost", sa.Numeric(), nullable=True))
    op.add_column("task_runs", sa.Column("captcha_cost", sa.Numeric(), nullable=True))


def downgrade() -> None:
    op.drop_column("task_runs", "captcha_cost")
    op.drop_column("task_runs", "proxy_cost")
    op.drop_column("task_runs", "llm_cost")
