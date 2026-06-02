"""Regression test for SKY-10591: GET /v1/scripts returned HTTP 500 because the
aggregate subquery used .filter_by() on a Core-level select, which cannot
resolve organization_id from the limited entity namespace."""

from sqlalchemy import and_, func, select
from sqlalchemy.dialects import postgresql

from skyvern.forge.sdk.db.models import ScriptModel


def test_get_scripts_subquery_compiles() -> None:
    """The aggregate subquery that finds the latest version per script_id must
    compile without raising 'has no property organization_id'."""
    organization_id = "o_test"

    latest_versions_subquery = (
        select(ScriptModel.script_id, func.max(ScriptModel.version).label("latest_version"))
        .filter(ScriptModel.organization_id == organization_id)
        .filter(ScriptModel.deleted_at.is_(None))
        .group_by(ScriptModel.script_id)
        .subquery()
    )

    get_scripts_query = (
        select(ScriptModel)
        .join(
            latest_versions_subquery,
            and_(
                ScriptModel.script_id == latest_versions_subquery.c.script_id,
                ScriptModel.version == latest_versions_subquery.c.latest_version,
            ),
        )
        .filter(ScriptModel.organization_id == organization_id)
        .filter(ScriptModel.deleted_at.is_(None))
        .order_by(ScriptModel.created_at.desc())
        .limit(10)
        .offset(0)
    )

    compiled = get_scripts_query.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "scripts" in sql
    assert "latest_version" in sql
    assert "organization_id" in sql


def test_get_scripts_subquery_filter_by_fails() -> None:
    """Demonstrate that .filter_by() on the aggregate subquery raises the
    original error — proving the regression exists without the fix."""
    organization_id = "o_test"

    try:
        subquery = (
            select(ScriptModel.script_id, func.max(ScriptModel.version).label("latest_version"))
            .filter_by(organization_id=organization_id)
            .filter(ScriptModel.deleted_at.is_(None))
            .group_by(ScriptModel.script_id)
            .subquery()
        )

        query = (
            select(ScriptModel)
            .join(
                subquery,
                and_(
                    ScriptModel.script_id == subquery.c.script_id,
                    ScriptModel.version == subquery.c.latest_version,
                ),
            )
            .filter_by(organization_id=organization_id)
            .filter(ScriptModel.deleted_at.is_(None))
        )
        query.compile(dialect=postgresql.dialect())
        compiled_ok = True
    except Exception:
        compiled_ok = False

    assert not compiled_ok, (
        ".filter_by() on a Core-level aggregate select should fail — "
        "if this passes, SQLAlchemy behavior changed and the negative test is stale"
    )
