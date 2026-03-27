from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from skyvern.forge.sdk.db.agent_db import _script_run_with_filter
from skyvern.forge.sdk.db.models import WorkflowRunModel


def _compile(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_script_run_with_filter_includes_null_and_code_modes() -> None:
    statement = select(WorkflowRunModel.workflow_run_id).where(_script_run_with_filter())
    compiled = _compile(statement)

    assert 'workflow_runs.run_with IN (\'code\', \'code_v2\')' in compiled
    assert "workflow_runs.run_with IS NULL" in compiled


def test_script_run_with_filter_excludes_agent_mode() -> None:
    statement = select(WorkflowRunModel.workflow_run_id).where(_script_run_with_filter())
    compiled = _compile(statement)

    assert "'agent'" not in compiled
