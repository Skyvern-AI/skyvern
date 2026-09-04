"""grant the Estuary CDC role SELECT on three app tables missing from the warehouse mirror

Revision ID: 456c370b0f7d
Revises: ac35398557c1
Create Date: 2026-09-04T06:08:50.733883+00:00

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "456c370b0f7d"
down_revision: str | None = "ac35398557c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CDC_ROLE = "flow_capture"
_PUBLICATION = "flow_publication"
_MIRRORED_TABLES = (
    "user_onboarding",
    "recovery_guidance_experiment_assignments",
    "workflow_tag_events",
)


def upgrade() -> None:
    for table in _MIRRORED_TABLES:
        op.execute(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_CDC_ROLE}') THEN
                RAISE NOTICE '{_CDC_ROLE} role not found, skipping grant on {table} (expected on local/OSS)';
                RETURN;
            END IF;
            IF to_regclass('public.{table}') IS NULL THEN
                RAISE NOTICE 'public.{table} not found, skipping grant';
                RETURN;
            END IF;

            GRANT SELECT ON public.{table} TO {_CDC_ROLE};

            IF EXISTS (SELECT 1 FROM pg_publication WHERE pubname = '{_PUBLICATION}' AND NOT puballtables) THEN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_publication_tables
                    WHERE pubname = '{_PUBLICATION}' AND schemaname = 'public' AND tablename = '{table}'
                ) THEN
                    BEGIN
                        ALTER PUBLICATION {_PUBLICATION} ADD TABLE public.{table};
                    EXCEPTION WHEN insufficient_privilege THEN
                        RAISE WARNING 'cannot alter {_PUBLICATION} as %; run manually as its owner: '
                                      'ALTER PUBLICATION {_PUBLICATION} ADD TABLE public.{table};', current_user;
                    END;
                END IF;
            END IF;
        END $$;
        """)


def downgrade() -> None:
    pass
