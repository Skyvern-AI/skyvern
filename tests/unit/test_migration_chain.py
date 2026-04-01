"""Regression tests for Alembic migration dependency chains.

SKY-8652: Migration 9a5230dbf85e (add_run_history_columns_to_task_runs) was failing in
CI with "relation task_runs does not exist" because its down_revision was wired to a
revision that did not include c00b2e3b62ec (add_task_runs) in its ancestor chain.

These tests verify that any migration touching task_runs has the table-creation
migration as an ancestor, preventing a repeat of SKY-8652.
"""

from __future__ import annotations

from alembic.config import Config
from alembic.script import ScriptDirectory


def _get_script_dir() -> ScriptDirectory:
    cfg = Config("alembic.ini")
    return ScriptDirectory.from_config(cfg)


def _get_all_ancestors(rev_id: str, script: ScriptDirectory) -> set[str]:
    """Return the full set of ancestor revision IDs for a given revision."""
    ancestors: set[str] = set()
    queue = [rev_id]
    while queue:
        current = queue.pop()
        if current in ancestors:
            continue
        ancestors.add(current)
        rev = script.get_revision(current)
        if rev and rev.down_revision:
            if isinstance(rev.down_revision, (list, tuple)):
                queue.extend(rev.down_revision)
            else:
                queue.append(rev.down_revision)
    return ancestors


# ── Revision ID constants ────────────────────────────────────────────────────
# c00b2e3b62ec: creates the task_runs table (add_task_runs, 2025-02-07)
_TASK_RUNS_CREATION_REV = "c00b2e3b62ec"

# 9a5230dbf85e: adds columns + indexes to task_runs (SKY-8652 failing migration)
_TASK_RUNS_COLUMNS_REV = "9a5230dbf85e"


def test_task_runs_creation_migration_exists() -> None:
    """The task_runs table-creation migration must be present in the migration graph."""
    script = _get_script_dir()
    rev = script.get_revision(_TASK_RUNS_CREATION_REV)
    assert rev is not None, (
        f"Migration {_TASK_RUNS_CREATION_REV} (add_task_runs) not found. "
        "It must exist so the task_runs table is created before later migrations run."
    )


def test_task_runs_columns_migration_has_table_creation_as_ancestor() -> None:
    """SKY-8652 regression: 9a5230dbf85e must descend from c00b2e3b62ec.

    When 9a5230dbf85e was originally wired with an incorrect down_revision, the
    task_runs table-creation migration (c00b2e3b62ec) was not in its ancestor chain.
    On a fresh CI database this caused:

        psycopg.errors.UndefinedTable: relation "task_runs" does not exist
        [SQL: CREATE INDEX CONCURRENTLY ... ON task_runs ...]

    Verifying the ancestor relationship here ensures the dependency chain stays
    correct even if future rebases or merges change the revision graph.
    """
    script = _get_script_dir()
    ancestors = _get_all_ancestors(_TASK_RUNS_COLUMNS_REV, script)
    assert _TASK_RUNS_CREATION_REV in ancestors, (
        f"Migration {_TASK_RUNS_COLUMNS_REV} (add_run_history_columns_to_task_runs) "
        f"does not have {_TASK_RUNS_CREATION_REV} (add_task_runs) as an ancestor. "
        "This means task_runs may not exist when the columns migration runs on a fresh "
        "database. Fix by ensuring down_revision traces back to the table-creation migration."
    )


def test_single_alembic_head() -> None:
    """There must be exactly one Alembic head.

    Multiple heads cause 'alembic upgrade head' to raise
    'Multiple head revisions are present for given argument head'.
    A merge migration must be added to resolve divergent branches before merging a PR.
    """
    script = _get_script_dir()
    heads = script.get_heads()
    assert len(heads) == 1, (
        f"Expected exactly 1 Alembic head, found {len(heads)}: {heads}. "
        "Run 'alembic merge heads -m \"merge heads\"' to create a merge migration."
    )
