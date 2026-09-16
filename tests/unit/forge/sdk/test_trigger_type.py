from skyvern.constants import SKYVERN_MCP_USER_AGENT, SKYVERN_UI_USER_AGENT
from skyvern.forge.sdk.db.enums import (
    WorkflowRunTriggerType,
    is_job_recipe_workflow_run_trigger_type,
)
from skyvern.forge.sdk.routes.trigger_type import workflow_run_trigger_type_from_user_agent


def test_workflow_run_trigger_type_from_user_agent_ui_returns_manual() -> None:
    assert workflow_run_trigger_type_from_user_agent(SKYVERN_UI_USER_AGENT) == WorkflowRunTriggerType.manual


def test_workflow_run_trigger_type_from_user_agent_mcp_returns_mcp() -> None:
    assert workflow_run_trigger_type_from_user_agent(SKYVERN_MCP_USER_AGENT) == WorkflowRunTriggerType.mcp


def test_workflow_run_trigger_type_from_user_agent_none_returns_api() -> None:
    assert workflow_run_trigger_type_from_user_agent(None) == WorkflowRunTriggerType.api


def test_workflow_run_trigger_type_from_user_agent_unknown_returns_api() -> None:
    assert workflow_run_trigger_type_from_user_agent("some-other-client") == WorkflowRunTriggerType.api


def test_is_job_recipe_workflow_run_trigger_type_returns_true_for_recipe_triggers() -> None:
    assert is_job_recipe_workflow_run_trigger_type(WorkflowRunTriggerType.job_recipe_extract)
    assert is_job_recipe_workflow_run_trigger_type(WorkflowRunTriggerType.job_recipe_apply)


def test_is_job_recipe_workflow_run_trigger_type_returns_false_for_other_triggers_and_none() -> None:
    for trigger_type in (
        WorkflowRunTriggerType.manual,
        WorkflowRunTriggerType.mcp,
        WorkflowRunTriggerType.api,
        WorkflowRunTriggerType.scheduled,
        WorkflowRunTriggerType.webhook,
        None,
    ):
        assert not is_job_recipe_workflow_run_trigger_type(trigger_type)
