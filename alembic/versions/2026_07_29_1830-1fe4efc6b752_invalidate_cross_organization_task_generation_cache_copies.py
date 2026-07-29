"""invalidate cross organization task generation cache copies

Revision ID: 1fe4efc6b752
Revises: 304752078903
Create Date: 2026-07-29T18:30:17.232021+00:00

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1fe4efc6b752"
down_revision: Union[str, None] = "304752078903"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        WITH RECURSIVE tainted AS (
            SELECT child.task_generation_id
              FROM task_generations child
              JOIN task_generations parent
                ON child.source_task_generation_id = parent.task_generation_id
             WHERE parent.organization_id <> child.organization_id
            UNION
            SELECT c.task_generation_id
              FROM task_generations c
              JOIN tainted t
                ON c.source_task_generation_id = t.task_generation_id
        )
        UPDATE task_generations
           SET llm = NULL
         WHERE task_generation_id IN (SELECT task_generation_id FROM tainted);
        """
    )


def downgrade() -> None:
    pass
