"""grant public schema privileges and default privileges to the k8s app roles

Revision ID: 1915b0e1126e
Revises: 2b8e37d98d97
Create Date: 2026-07-15T02:08:43.909060+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1915b0e1126e"
down_revision: Union[str, None] = "2b8e37d98d97"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_APP_ROLES = ("k8s_worker", "k8s_browser_sessions")


def upgrade() -> None:
    for app_role in _APP_ROLES:
        op.execute(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{app_role}') THEN
                RAISE NOTICE '{app_role} role not found, skipping grants (expected on local/OSS)';
                RETURN;
            END IF;

            GRANT USAGE ON SCHEMA public TO {app_role};
            GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES    IN SCHEMA public TO {app_role};
            GRANT USAGE, SELECT, UPDATE                 ON ALL SEQUENCES IN SCHEMA public TO {app_role};
            GRANT EXECUTE                               ON ALL FUNCTIONS IN SCHEMA public TO {app_role};

            -- current_user can always ALTER its own default privileges.
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
                'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {app_role}',
                current_user
            );
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
                'GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {app_role}',
                current_user
            );
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
                'GRANT EXECUTE ON FUNCTIONS TO {app_role}',
                current_user
            );

            -- Also set defaults FOR ROLE postgres so a future session-user
            -- rotation or a one-off hotfix run as postgres still inherits the
            -- rule. Requires current_user to be a member of postgres (or a
            -- superuser, which pg_has_role reports as true for every role).
            IF current_user <> 'postgres'
               AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres')
               AND pg_has_role(current_user, 'postgres', 'MEMBER') THEN
                ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
                    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {app_role};
                ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
                    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {app_role};
                ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
                    GRANT EXECUTE ON FUNCTIONS TO {app_role};
            END IF;
        END $$;
        """)


def downgrade() -> None:
    pass
