"""grant the app_hex analytics role SELECT on the stranded analytics tables

Revision ID: 8a7f32c5f807
Revises: 88cb50acd5f4
Create Date: 2026-08-29T19:38:51.238403+00:00

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8a7f32c5f807"
down_revision: Union[str, None] = "88cb50acd5f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ANALYTICS_ROLE = "app_hex"
_ANALYTICS_READABLE_TABLES = (
    "workflow_tag_events",
    "workflow_run_tag_events",
    "task_proxy_usage",
    "recovery_guidance_experiment_assignments",
)


def upgrade() -> None:
    grants = "\n".join(
        f"""
        IF to_regclass('public.{table}') IS NOT NULL THEN
            GRANT SELECT ON public.{table} TO {_ANALYTICS_ROLE};
        END IF;"""
        for table in _ANALYTICS_READABLE_TABLES
    )
    op.execute(f"""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_ANALYTICS_ROLE}') THEN
            RAISE NOTICE '{_ANALYTICS_ROLE} role not found, skipping grants (expected on local/OSS)';
            RETURN;
        END IF;

        GRANT USAGE ON SCHEMA public TO {_ANALYTICS_ROLE};
        {grants}
    END $$;
    """)


def downgrade() -> None:
    pass
