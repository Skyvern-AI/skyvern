"""Tests for scripts/generate_oss_migration.py."""

from __future__ import annotations

import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.generate_oss_migration import (
    CLOUD_ONLY_TABLES,
    classify_migration,
    find_oss_head,
    generate_migration_file,
    main,
    parse_migration,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SCHEMA_MIGRATION = textwrap.dedent('''\
    """add script pinning columns

    Revision ID: 1091575790eb
    Revises: 2c6e5b2d0bc9
    Create Date: 2026-03-12 00:36:57.911074+00:00

    """

    from typing import Sequence, Union

    import sqlalchemy as sa

    from alembic import op

    # revision identifiers, used by Alembic.
    revision: str = "1091575790eb"
    down_revision: Union[str, None] = "2c6e5b2d0bc9"
    branch_labels: Union[str, Sequence[str], None] = None
    depends_on: Union[str, Sequence[str], None] = None


    def upgrade() -> None:
        op.add_column("workflow_scripts", sa.Column("is_pinned", sa.Boolean(), server_default="false", nullable=False))
        op.add_column("workflow_scripts", sa.Column("pinned_at", sa.DateTime(), nullable=True))
        op.add_column("workflow_scripts", sa.Column("pinned_by", sa.String(), nullable=True))


    def downgrade() -> None:
        op.drop_column("workflow_scripts", "pinned_by")
        op.drop_column("workflow_scripts", "pinned_at")
        op.drop_column("workflow_scripts", "is_pinned")
''')

DATA_MIGRATION = textwrap.dedent('''\
    """add partial indexes on artifacts

    Revision ID: b7d5e082f365
    Revises: bcc728d65b57
    Create Date: 2026-03-05 20:45:00.000000+00:00

    """

    from typing import Sequence, Union

    import sqlalchemy as sa

    from alembic import op

    revision: str = "b7d5e082f365"
    down_revision: Union[str, None] = "bcc728d65b57"
    branch_labels: Union[str, Sequence[str], None] = None
    depends_on: Union[str, Sequence[str], None] = None


    def upgrade() -> None:
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_foo ON artifacts (id)")


    def downgrade() -> None:
        op.execute("DROP INDEX IF EXISTS idx_foo")
''')

CLOUD_ONLY_MIGRATION = textwrap.dedent('''\
    """add pricing tier column

    Revision ID: abc123def456
    Revises: 111222333444
    Create Date: 2026-03-10 12:00:00.000000+00:00

    """

    from typing import Sequence, Union

    import sqlalchemy as sa

    from alembic import op

    revision: str = "abc123def456"
    down_revision: Union[str, None] = "111222333444"
    branch_labels: Union[str, Sequence[str], None] = None
    depends_on: Union[str, Sequence[str], None] = None


    def upgrade() -> None:
        op.add_column("organization_pricing", sa.Column("tier", sa.String(), nullable=True))


    def downgrade() -> None:
        op.drop_column("organization_pricing", "tier")
''')

INDEX_MIGRATION = textwrap.dedent('''\
    """add index on workflows

    Revision ID: idx123idx456
    Revises: 999888777666
    Create Date: 2026-03-10 12:00:00.000000+00:00

    """

    from typing import Sequence, Union

    import sqlalchemy as sa

    from alembic import op

    revision: str = "idx123idx456"
    down_revision: Union[str, None] = "999888777666"
    branch_labels: Union[str, Sequence[str], None] = None
    depends_on: Union[str, Sequence[str], None] = None


    def upgrade() -> None:
        op.create_index("ix_workflows_status", "workflows", ["status"])


    def downgrade() -> None:
        op.drop_index("ix_workflows_status", table_name="workflows")
''')

CLOUD_INDEX_MIGRATION = textwrap.dedent('''\
    """add index on organization_pricing

    Revision ID: cidx12cidx34
    Revises: 555444333222
    Create Date: 2026-03-10 12:00:00.000000+00:00

    """

    from typing import Sequence, Union

    import sqlalchemy as sa

    from alembic import op

    revision: str = "cidx12cidx34"
    down_revision: Union[str, None] = "555444333222"
    branch_labels: Union[str, Sequence[str], None] = None
    depends_on: Union[str, Sequence[str], None] = None


    def upgrade() -> None:
        op.create_index("ix_org_pricing_tier", "organization_pricing", ["tier"])


    def downgrade() -> None:
        op.drop_index("ix_org_pricing_tier", table_name="organization_pricing")
''')

MULTI_TABLE_MIGRATION = textwrap.dedent('''\
    """add columns to mixed tables

    Revision ID: mixed123mixed
    Revises: aaa111bbb222
    Create Date: 2026-03-10 12:00:00.000000+00:00

    """

    from typing import Sequence, Union

    import sqlalchemy as sa

    from alembic import op

    revision: str = "mixed123mixed"
    down_revision: Union[str, None] = "aaa111bbb222"
    branch_labels: Union[str, Sequence[str], None] = None
    depends_on: Union[str, Sequence[str], None] = None


    def upgrade() -> None:
        op.add_column("workflows", sa.Column("new_flag", sa.Boolean(), nullable=True))
        op.add_column("organization_pricing", sa.Column("discount", sa.Float(), nullable=True))


    def downgrade() -> None:
        op.drop_column("organization_pricing", "discount")
        op.drop_column("workflows", "new_flag")
''')


def _write_migration(tmp_path: Path, filename: str, content: str) -> Path:
    p = tmp_path / filename
    p.write_text(content)
    return p


def _create_oss_versions_dir(tmp_path: Path) -> Path:
    """Create a fake OSS versions dir with a simple chain: rev_a -> rev_b -> rev_c (head)."""
    versions_dir = tmp_path / "oss_versions"
    versions_dir.mkdir()

    for rev_id, down_rev, desc in [
        ("aaaaaaaaaaaa", None, "initial"),
        ("bbbbbbbbbbbb", "aaaaaaaaaaaa", "add_workflows"),
        ("cccccccccccc", "bbbbbbbbbbbb", "add_artifacts"),
    ]:
        down_rev_repr = f'"{down_rev}"' if down_rev else "None"
        content = textwrap.dedent(f'''\
            """migration {desc}"""
            from typing import Sequence, Union
            from alembic import op
            import sqlalchemy as sa

            revision: str = "{rev_id}"
            down_revision: Union[str, None] = {down_rev_repr}
            branch_labels: Union[str, Sequence[str], None] = None
            depends_on: Union[str, Sequence[str], None] = None

            def upgrade() -> None:
                pass

            def downgrade() -> None:
                pass
        ''')
        (versions_dir / f"2026_01_0{len(rev_id)}_0000-{rev_id}_{desc}.py").write_text(content)

    return versions_dir


# ---------------------------------------------------------------------------
# Tests: parse_migration
# ---------------------------------------------------------------------------


class TestCloudOnlyTables:
    """Guard against CLOUD_ONLY_TABLES drifting out of sync with actual cloud models."""

    def test_matches_cloud_model_files(self) -> None:
        tables_from_code: set[str] = set()
        cloud_model_files = [
            Path("cloud/db/db_models.py"),
            Path("cloud/db/models/feature_config.py"),
        ]
        for path in cloud_model_files:
            for line in path.read_text().splitlines():
                if "__tablename__" in line and '"' in line:
                    tables_from_code.add(line.split('"')[1])

        assert tables_from_code == CLOUD_ONLY_TABLES, (
            f"CLOUD_ONLY_TABLES in generate_oss_migration.py is out of sync.\n"
            f"  In code but not in set: {tables_from_code - CLOUD_ONLY_TABLES}\n"
            f"  In set but not in code: {CLOUD_ONLY_TABLES - tables_from_code}"
        )


class TestParseMigration:
    def test_parses_schema_migration(self, tmp_path: Path) -> None:
        f = _write_migration(tmp_path, "schema.py", SCHEMA_MIGRATION)
        info = parse_migration(f)

        assert info.revision == "1091575790eb"
        assert info.down_revision == "2c6e5b2d0bc9"
        assert info.message == "add script pinning columns"
        assert "op.add_column" in info.upgrade_source
        assert "op.drop_column" in info.downgrade_source
        assert info.tables_referenced == {"workflow_scripts"}
        assert info.has_raw_sql is False

    def test_parses_data_migration(self, tmp_path: Path) -> None:
        f = _write_migration(tmp_path, "data.py", DATA_MIGRATION)
        info = parse_migration(f)

        assert info.revision == "b7d5e082f365"
        assert info.has_raw_sql is True
        assert "op.execute" in info.upgrade_source

    def test_parses_cloud_only_migration(self, tmp_path: Path) -> None:
        f = _write_migration(tmp_path, "cloud_only.py", CLOUD_ONLY_MIGRATION)
        info = parse_migration(f)

        assert info.tables_referenced == {"organization_pricing"}
        assert info.tables_referenced.issubset(CLOUD_ONLY_TABLES)

    def test_parses_multi_table_migration(self, tmp_path: Path) -> None:
        f = _write_migration(tmp_path, "multi.py", MULTI_TABLE_MIGRATION)
        info = parse_migration(f)

        assert info.tables_referenced == {"workflows", "organization_pricing"}

    def test_parses_index_migration_tables(self, tmp_path: Path) -> None:
        """create_index/drop_index should extract the table name, not the index name."""
        f = _write_migration(tmp_path, "idx.py", INDEX_MIGRATION)
        info = parse_migration(f)

        assert "workflows" in info.tables_referenced
        # The index name should NOT appear as a table reference
        assert "ix_workflows_status" not in info.tables_referenced

    def test_cloud_only_index_is_skip(self, tmp_path: Path) -> None:
        """Index on a cloud-only table should be classified as skip."""
        f = _write_migration(tmp_path, "cidx.py", CLOUD_INDEX_MIGRATION)
        info = parse_migration(f)

        assert info.tables_referenced == {"organization_pricing"}
        assert classify_migration(info) == "skip"

    def test_handles_op_f_index_name(self, tmp_path: Path) -> None:
        """op.create_index(op.f("ix_name"), "table", ...) should extract the table, not the index."""
        content = textwrap.dedent('''\
            """add index with op.f"""
            from typing import Sequence, Union
            from alembic import op
            import sqlalchemy as sa

            revision: str = "opf123opf456"
            down_revision: Union[str, None] = "000000000000"
            branch_labels: Union[str, Sequence[str], None] = None
            depends_on: Union[str, Sequence[str], None] = None

            def upgrade() -> None:
                op.create_index(op.f("ix_workflows_workflow_id"), "workflows", ["workflow_id"])

            def downgrade() -> None:
                op.drop_index(op.f("ix_workflows_workflow_id"), table_name="workflows")
        ''')
        f = _write_migration(tmp_path, "opf.py", content)
        info = parse_migration(f)

        assert "workflows" in info.tables_referenced
        assert "ix_workflows_workflow_id" not in info.tables_referenced

    def test_drop_index_op_f_with_table_name_kwarg(self, tmp_path: Path) -> None:
        """op.drop_index(op.f("ix"), table_name="tbl") must capture the table even with nested parens."""
        content = textwrap.dedent('''\
            """drop index only"""
            from typing import Sequence, Union
            from alembic import op

            revision: str = "dpf123dpf456"
            down_revision: Union[str, None] = "000000000000"
            branch_labels: Union[str, Sequence[str], None] = None
            depends_on: Union[str, Sequence[str], None] = None

            def upgrade() -> None:
                pass

            def downgrade() -> None:
                op.drop_index(op.f("ix_sessions_profile_id"), table_name="persistent_browser_sessions")
        ''')
        f = _write_migration(tmp_path, "drop_opf.py", content)
        info = parse_migration(f)

        assert "persistent_browser_sessions" in info.tables_referenced
        assert "ix_sessions_profile_id" not in info.tables_referenced

    def test_multiline_op_calls(self, tmp_path: Path) -> None:
        """Multiline op calls (common from alembic --autogenerate) must be handled."""
        content = textwrap.dedent('''\
            """multiline ops"""
            from typing import Sequence, Union
            from alembic import op

            revision: str = "ml1234ml5678"
            down_revision: Union[str, None] = "000000000000"
            branch_labels: Union[str, Sequence[str], None] = None
            depends_on: Union[str, Sequence[str], None] = None

            def upgrade() -> None:
                op.create_index(
                    op.f("ix_sessions_profile_id"),
                    "persistent_browser_sessions",
                    ["browser_profile_id"],
                )

            def downgrade() -> None:
                op.drop_index(
                    op.f("ix_sessions_profile_id"),
                    table_name="persistent_browser_sessions"
                )
        ''')
        f = _write_migration(tmp_path, "multiline.py", content)
        info = parse_migration(f)

        assert "persistent_browser_sessions" in info.tables_referenced

    def test_no_duplicate_sa_import(self, tmp_path: Path) -> None:
        """import sqlalchemy as sa should not appear in extra imports (already in template)."""
        f = _write_migration(tmp_path, "schema.py", SCHEMA_MIGRATION)
        info = parse_migration(f)

        assert not any("import sqlalchemy as sa" == imp.strip() for imp in info.imports)

    def test_foreign_key_constraint_not_captured_as_table(self, tmp_path: Path) -> None:
        """op.create_foreign_key("fk_name", ...) should not capture the constraint name."""
        content = textwrap.dedent('''\
            """add foreign key"""
            from typing import Sequence, Union
            from alembic import op
            import sqlalchemy as sa

            revision: str = "fk1234fk5678"
            down_revision: Union[str, None] = "000000000000"
            branch_labels: Union[str, Sequence[str], None] = None
            depends_on: Union[str, Sequence[str], None] = None

            def upgrade() -> None:
                op.create_foreign_key("fk_task_workflow", "tasks", "workflows", ["workflow_id"], ["id"])

            def downgrade() -> None:
                op.drop_constraint("fk_task_workflow", "tasks", type_="foreignkey")
        ''')
        f = _write_migration(tmp_path, "fk.py", content)
        info = parse_migration(f)

        assert "fk_task_workflow" not in info.tables_referenced
        # The actual table names should be captured from create_foreign_key's 2nd and 3rd args
        assert "tasks" in info.tables_referenced
        assert "workflows" in info.tables_referenced

    def test_parses_table_names_with_digits(self, tmp_path: Path) -> None:
        """Table names containing digits (e.g., tasks_v2) should be detected."""
        content = textwrap.dedent('''\
            """add column to tasks_v2

            Revision ID: dig123dig456
            Revises: 000000000000
            Create Date: 2026-03-10 12:00:00.000000+00:00

            """

            from typing import Sequence, Union

            import sqlalchemy as sa

            from alembic import op

            revision: str = "dig123dig456"
            down_revision: Union[str, None] = "000000000000"
            branch_labels: Union[str, Sequence[str], None] = None
            depends_on: Union[str, Sequence[str], None] = None


            def upgrade() -> None:
                op.add_column("tasks_v2", sa.Column("priority", sa.Integer(), nullable=True))


            def downgrade() -> None:
                op.drop_column("tasks_v2", "priority")
        ''')
        f = _write_migration(tmp_path, "digits.py", content)
        info = parse_migration(f)

        assert "tasks_v2" in info.tables_referenced

    def test_parses_tuple_down_revision(self, tmp_path: Path) -> None:
        """Merge migrations with tuple down_revision should be parsed correctly."""
        content = textwrap.dedent('''\
            """merge heads

            Revision ID: merge1merge2
            Revises: aaa111, bbb222
            Create Date: 2026-03-10 12:00:00.000000+00:00

            """

            from typing import Sequence, Union

            import sqlalchemy as sa

            from alembic import op

            revision: str = "merge1merge2"
            down_revision: Union[str, None] = ("aaa111bbb222", "ccc333ddd444")
            branch_labels: Union[str, Sequence[str], None] = None
            depends_on: Union[str, Sequence[str], None] = None


            def upgrade() -> None:
                pass


            def downgrade() -> None:
                pass
        ''')
        f = _write_migration(tmp_path, "merge.py", content)
        info = parse_migration(f)

        assert info.revision == "merge1merge2"
        assert info.down_revision == ("aaa111bbb222", "ccc333ddd444")

    def test_raises_on_missing_revision(self, tmp_path: Path) -> None:
        content = textwrap.dedent('''\
            """no revision"""
            from alembic import op

            def upgrade() -> None:
                pass

            def downgrade() -> None:
                pass
        ''')
        f = _write_migration(tmp_path, "bad.py", content)
        with pytest.raises(ValueError, match="Could not find 'revision'"):
            parse_migration(f)


# ---------------------------------------------------------------------------
# Tests: find_oss_head
# ---------------------------------------------------------------------------


class TestFindOssHead:
    def test_finds_head_in_chain(self, tmp_path: Path) -> None:
        versions_dir = _create_oss_versions_dir(tmp_path)
        head = find_oss_head(versions_dir)
        assert head == "cccccccccccc"

    def test_raises_on_empty_dir(self, tmp_path: Path) -> None:
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        with pytest.raises(ValueError, match="No alembic head found"):
            find_oss_head(empty_dir)

    def test_raises_on_missing_dir(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            find_oss_head(tmp_path / "nonexistent")

    def test_handles_merge_migration_with_tuple_down_revision(self, tmp_path: Path) -> None:
        """Merge migrations have down_revision = ("rev_a", "rev_b") as a tuple."""
        versions_dir = tmp_path / "versions"
        versions_dir.mkdir()

        # Two branches
        for rev_id, down_rev, desc in [
            ("aaaaaaaaaaaa", None, "initial"),
            ("bbbbbbbbbbbb", "aaaaaaaaaaaa", "branch_a"),
            ("cccccccccccc", "aaaaaaaaaaaa", "branch_b"),
        ]:
            down_repr = f'"{down_rev}"' if down_rev else "None"
            content = textwrap.dedent(f'''\
                """migration {desc}"""
                from typing import Sequence, Union
                revision: str = "{rev_id}"
                down_revision: Union[str, None] = {down_repr}
                branch_labels: Union[str, Sequence[str], None] = None
                depends_on: Union[str, Sequence[str], None] = None
                def upgrade() -> None:
                    pass
                def downgrade() -> None:
                    pass
            ''')
            (versions_dir / f"{rev_id}_{desc}.py").write_text(content)

        # Merge migration
        merge = textwrap.dedent('''\
            """merge heads"""
            from typing import Sequence, Union
            revision: str = "dddddddddddd"
            down_revision: Union[str, None] = ("bbbbbbbbbbbb", "cccccccccccc")
            branch_labels: Union[str, Sequence[str], None] = None
            depends_on: Union[str, Sequence[str], None] = None
            def upgrade() -> None:
                pass
            def downgrade() -> None:
                pass
        ''')
        (versions_dir / "dddddddddddd_merge.py").write_text(merge)

        head = find_oss_head(versions_dir)
        assert head == "dddddddddddd"

    def test_raises_on_multiple_heads(self, tmp_path: Path) -> None:
        versions_dir = _create_oss_versions_dir(tmp_path)
        # Add a second head that branches from 'bbbbbbbbbbbb'
        branched = textwrap.dedent('''\
            """branched migration"""
            from typing import Sequence, Union
            from alembic import op
            import sqlalchemy as sa

            revision: str = "dddddddddddd"
            down_revision: Union[str, None] = "bbbbbbbbbbbb"
            branch_labels: Union[str, Sequence[str], None] = None
            depends_on: Union[str, Sequence[str], None] = None

            def upgrade() -> None:
                pass

            def downgrade() -> None:
                pass
        ''')
        (versions_dir / "2026_01_05_0000-dddddddddddd_branched.py").write_text(branched)
        with pytest.raises(ValueError, match="Multiple alembic heads"):
            find_oss_head(versions_dir)


# ---------------------------------------------------------------------------
# Tests: classify_migration
# ---------------------------------------------------------------------------


class TestClassifyMigration:
    def test_schema_migration_is_sync(self, tmp_path: Path) -> None:
        f = _write_migration(tmp_path, "schema.py", SCHEMA_MIGRATION)
        info = parse_migration(f)
        assert classify_migration(info) == "sync"

    def test_data_migration_is_review(self, tmp_path: Path) -> None:
        f = _write_migration(tmp_path, "data.py", DATA_MIGRATION)
        info = parse_migration(f)
        assert classify_migration(info) == "review"

    def test_cloud_only_is_skip(self, tmp_path: Path) -> None:
        f = _write_migration(tmp_path, "cloud.py", CLOUD_ONLY_MIGRATION)
        info = parse_migration(f)
        assert classify_migration(info) == "skip"

    def test_mixed_tables_is_review(self, tmp_path: Path) -> None:
        """Migration touching both cloud and shared tables needs manual review."""
        f = _write_migration(tmp_path, "mixed.py", MULTI_TABLE_MIGRATION)
        info = parse_migration(f)
        assert classify_migration(info) == "review"

    def test_noop_migration_is_skip(self, tmp_path: Path) -> None:
        """Empty upgrade/downgrade with no table references should be skipped."""
        content = textwrap.dedent('''\
            """noop migration

            Revision ID: noop12noop34
            Revises: 000000000000
            Create Date: 2026-03-10 12:00:00.000000+00:00

            """

            from typing import Sequence, Union

            import sqlalchemy as sa

            from alembic import op

            revision: str = "noop12noop34"
            down_revision: Union[str, None] = "000000000000"
            branch_labels: Union[str, Sequence[str], None] = None
            depends_on: Union[str, Sequence[str], None] = None


            def upgrade() -> None:
                pass


            def downgrade() -> None:
                pass
        ''')
        f = _write_migration(tmp_path, "noop.py", content)
        info = parse_migration(f)
        assert classify_migration(info) == "skip"

    def test_no_tables_detected_is_review(self, tmp_path: Path) -> None:
        """Non-trivial migration with no detected table references should be flagged for review."""
        content = textwrap.dedent('''\
            """helper function migration

            Revision ID: notbl1notbl2
            Revises: 000000000000
            Create Date: 2026-03-10 12:00:00.000000+00:00

            """

            from typing import Sequence, Union

            import sqlalchemy as sa

            from alembic import op

            revision: str = "notbl1notbl2"
            down_revision: Union[str, None] = "000000000000"
            branch_labels: Union[str, Sequence[str], None] = None
            depends_on: Union[str, Sequence[str], None] = None


            def upgrade() -> None:
                apply_custom_schema_changes()


            def downgrade() -> None:
                revert_custom_schema_changes()
        ''')
        f = _write_migration(tmp_path, "rename.py", content)
        info = parse_migration(f)
        assert classify_migration(info) == "review"


# ---------------------------------------------------------------------------
# Tests: generate_migration_file
# ---------------------------------------------------------------------------


class TestGenerateMigrationFile:
    def test_generates_valid_migration(self, tmp_path: Path) -> None:
        cloud_file = _write_migration(tmp_path, "cloud.py", SCHEMA_MIGRATION)
        info = parse_migration(cloud_file)
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        fixed_date = datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = generate_migration_file(
            info=info,
            oss_down_revision="cccccccccccc",
            output_dir=output_dir,
            revision_id="aabbccddeeff",
            date=fixed_date,
        )

        assert result.exists()
        content = result.read_text()

        # Check revision chain
        assert 'revision: str = "aabbccddeeff"' in content
        assert 'down_revision: Union[str, None] = "cccccccccccc"' in content

        # Check operations preserved
        assert "op.add_column" in content
        assert "op.drop_column" in content
        assert "workflow_scripts" in content
        assert "is_pinned" in content

        # Check filename format
        assert result.name.startswith("2026_03_15_1200-aabbccddeeff_")

        # Check it's valid Python
        compile(content, str(result), "exec")

    def test_generated_file_has_correct_structure(self, tmp_path: Path) -> None:
        cloud_file = _write_migration(tmp_path, "cloud.py", SCHEMA_MIGRATION)
        info = parse_migration(cloud_file)
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        result = generate_migration_file(
            info=info,
            oss_down_revision="head123head12",
            output_dir=output_dir,
            revision_id="test12test12",
            date=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        )

        content = result.read_text()

        # Must have standard imports
        assert "from typing import Sequence, Union" in content
        assert "import sqlalchemy as sa" in content
        assert "from alembic import op" in content

        # Must have both functions
        assert "def upgrade() -> None:" in content
        assert "def downgrade() -> None:" in content

        # Must NOT have cloud revision IDs
        assert "1091575790eb" not in content
        assert "2c6e5b2d0bc9" not in content

    def test_includes_non_standard_imports(self, tmp_path: Path) -> None:
        """Extra imports (e.g., dialect-specific) should be included in the generated file."""
        content = textwrap.dedent('''\
            """add jsonb column

            Revision ID: json12json34
            Revises: 000000000000
            Create Date: 2026-03-10 12:00:00.000000+00:00

            """

            from typing import Sequence, Union

            import sqlalchemy as sa

            from alembic import op
            from sqlalchemy.dialects import postgresql

            revision: str = "json12json34"
            down_revision: Union[str, None] = "000000000000"
            branch_labels: Union[str, Sequence[str], None] = None
            depends_on: Union[str, Sequence[str], None] = None


            def upgrade() -> None:
                op.add_column("workflows", sa.Column("metadata", postgresql.JSONB(), nullable=True))


            def downgrade() -> None:
                op.drop_column("workflows", "metadata")
        ''')
        f = _write_migration(tmp_path, "jsonb.py", content)
        info = parse_migration(f)
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        result = generate_migration_file(
            info=info,
            oss_down_revision="cccccccccccc",
            output_dir=output_dir,
            revision_id="gen123gen456",
            date=datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc),
        )

        generated_content = result.read_text()
        assert "from sqlalchemy.dialects import postgresql" in generated_content
        assert "postgresql.JSONB()" in generated_content

        # Verify it's valid Python
        compile(generated_content, str(result), "exec")

    def test_does_not_reuse_cloud_revision_id(self, tmp_path: Path) -> None:
        cloud_file = _write_migration(tmp_path, "cloud.py", SCHEMA_MIGRATION)
        info = parse_migration(cloud_file)
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        result = generate_migration_file(
            info=info,
            oss_down_revision="cccccccccccc",
            output_dir=output_dir,
        )

        content = result.read_text()
        # Auto-generated revision should NOT be the cloud one
        assert "1091575790eb" not in content


# ---------------------------------------------------------------------------
# Tests: main (CLI integration)
# ---------------------------------------------------------------------------


class TestMain:
    def test_dry_run(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        cloud_file = _write_migration(tmp_path, "cloud.py", SCHEMA_MIGRATION)
        versions_dir = _create_oss_versions_dir(tmp_path)

        result = main(
            [
                "--cloud-migration",
                str(cloud_file),
                "--oss-versions-dir",
                str(versions_dir),
                "--dry-run",
            ]
        )

        assert result == 0
        captured = capsys.readouterr()
        assert "Would generate" in captured.out
        assert "add script pinning columns" in captured.out
        # No files should have been created beyond the original 3
        assert len(list(versions_dir.glob("*.py"))) == 3

    def test_generates_migration(self, tmp_path: Path) -> None:
        cloud_file = _write_migration(tmp_path, "cloud.py", SCHEMA_MIGRATION)
        versions_dir = _create_oss_versions_dir(tmp_path)

        result = main(
            [
                "--cloud-migration",
                str(cloud_file),
                "--oss-versions-dir",
                str(versions_dir),
            ]
        )

        assert result == 0
        # Should have 4 files now (3 original + 1 generated)
        assert len(list(versions_dir.glob("*.py"))) == 4

    def test_skips_cloud_only(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        cloud_file = _write_migration(tmp_path, "cloud.py", CLOUD_ONLY_MIGRATION)
        versions_dir = _create_oss_versions_dir(tmp_path)

        result = main(
            [
                "--cloud-migration",
                str(cloud_file),
                "--oss-versions-dir",
                str(versions_dir),
            ]
        )

        assert result == 0
        captured = capsys.readouterr()
        assert "Skipped" in captured.out
        # No new files
        assert len(list(versions_dir.glob("*.py"))) == 3

    def test_chains_multiple_migrations(self, tmp_path: Path) -> None:
        file1 = _write_migration(tmp_path, "first.py", SCHEMA_MIGRATION)

        second_migration = textwrap.dedent('''\
            """add workflow status column

            Revision ID: 222222222222
            Revises: 1091575790eb
            Create Date: 2026-03-13 00:00:00.000000+00:00

            """

            from typing import Sequence, Union

            import sqlalchemy as sa

            from alembic import op

            revision: str = "222222222222"
            down_revision: Union[str, None] = "1091575790eb"
            branch_labels: Union[str, Sequence[str], None] = None
            depends_on: Union[str, Sequence[str], None] = None


            def upgrade() -> None:
                op.add_column("workflows", sa.Column("status_v2", sa.String(), nullable=True))


            def downgrade() -> None:
                op.drop_column("workflows", "status_v2")
        ''')
        file2 = _write_migration(tmp_path, "second.py", second_migration)

        versions_dir = _create_oss_versions_dir(tmp_path)

        result = main(
            [
                "--cloud-migration",
                str(file1),
                "--cloud-migration",
                str(file2),
                "--oss-versions-dir",
                str(versions_dir),
            ]
        )

        assert result == 0
        # 3 original + 2 generated
        all_files = list(versions_dir.glob("*.py"))
        assert len(all_files) == 5

        # Verify chain: find the first generated (points to original head),
        # then verify the second points to the first.
        generated = [f for f in all_files if "add_script_pinning" in f.name or "add_workflow_status" in f.name]
        assert len(generated) == 2

        parsed = [parse_migration(f) for f in generated]
        # One should point to the original OSS head, the other should chain off it
        by_down_rev = {p.down_revision: p for p in parsed}
        first_gen = by_down_rev["cccccccccccc"]  # Points to original OSS head
        assert first_gen.revision in by_down_rev  # Second migration chains off the first

    def test_warns_on_data_migration(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        cloud_file = _write_migration(tmp_path, "data.py", DATA_MIGRATION)
        versions_dir = _create_oss_versions_dir(tmp_path)

        result = main(
            [
                "--cloud-migration",
                str(cloud_file),
                "--oss-versions-dir",
                str(versions_dir),
            ]
        )

        assert result == 0
        captured = capsys.readouterr()
        assert "Needs manual review" in captured.out
        # Should still generate the file (with warning)
        assert len(list(versions_dir.glob("*.py"))) == 4
