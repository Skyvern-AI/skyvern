"""credentials partial-unique index on browser_profile_id (one credential per profile)

Revision ID: efd117d09dd9
Revises: b192c2ddb005
Create Date: 2026-07-24T18:02:38.554613+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "efd117d09dd9"
down_revision: Union[str, None] = "b192c2ddb005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX = "uq_credentials_browser_profile_id"


def upgrade() -> None:
    conn = op.get_bind()
    dups = [
        row[0]
        for row in conn.execute(
            sa.text(
                "SELECT browser_profile_id FROM credentials "
                "WHERE browser_profile_id IS NOT NULL AND deleted_at IS NULL "
                "GROUP BY browser_profile_id HAVING count(*) > 1"
            )
        )
    ]
    if dups:
        raise RuntimeError(
            "Cannot create the unique credentials(browser_profile_id) index: these browser_profile_ids "
            f"are linked by more than one live credential and must be deduplicated first: {dups}"
        )

    # The CONCURRENTLY build has a check-then-build window vs concurrent link writes, but it is safe on
    # our deploy model: migrations run BEFORE the new image serves traffic (cloud_docs/deploy/README.md —
    # smoke -> migrate -> deploy), and the credential<->profile link-write API arrives in this same PR, so
    # during the build only old code (no link API) is live and cannot create a competing link. A
    # hypothetical re-apply is covered by the dedup pre-check above plus the DROP-INVALID retry idiom below.
    with op.get_context().autocommit_block():
        # A prior interrupted CONCURRENTLY build can leave an INVALID index of this name. CREATE ...
        # IF NOT EXISTS matches by name regardless of validity and would skip recreation, silently
        # leaving one-credential-per-profile unenforced. Drop any leftover first so the build below
        # always produces a VALID index.
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
        op.execute(
            f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
            "ON credentials (browser_profile_id) "
            "WHERE browser_profile_id IS NOT NULL AND deleted_at IS NULL"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
