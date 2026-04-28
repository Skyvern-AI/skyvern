from unittest.mock import AsyncMock, MagicMock

from skyvern.forge import set_force_app_instance
from skyvern.forge.forge_app import ForgeApp


def create_forge_stub_app() -> ForgeApp:
    class _LazyNamespace:
        def __getattr__(self, name):
            value = AsyncMock()
            setattr(self, name, value)
            return value

    fake_app_module = ForgeApp()
    fake_app_module.DATABASE = _LazyNamespace()
    fake_app_module.WORKFLOW_CONTEXT_MANAGER = _LazyNamespace()
    fake_app_module.WORKFLOW_SERVICE = _LazyNamespace()
    fake_app_module.BROWSER_MANAGER = _LazyNamespace()
    fake_app_module.PERSISTENT_SESSIONS_MANAGER = _LazyNamespace()
    fake_app_module.ARTIFACT_MANAGER = _LazyNamespace()
    fake_app_module.AGENT_FUNCTION = _LazyNamespace()
    fake_app_module.AGENT_FUNCTION.validate_block_execution = AsyncMock()
    fake_app_module.AGENT_FUNCTION.validate_code_block = AsyncMock()
    # Sync method — _LazyNamespace would auto-mock it as AsyncMock and break
    # callers that unpack the return value. Match the real OSS default (None).
    fake_app_module.AGENT_FUNCTION.resolve_mcp_oauth_org_lookups = MagicMock(return_value=None)
    fake_app_module.agent = _LazyNamespace()
    fake_app_module.DATABASE.observer.update_workflow_run_block = AsyncMock()
    fake_app_module.DATABASE.observer.create_workflow_run_block = AsyncMock()
    fake_app_module.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter = AsyncMock()
    fake_app_module.DATABASE.tasks.get_last_task_for_workflow_run = AsyncMock()
    fake_app_module.DATABASE.workflow_runs.get_workflow_run = AsyncMock()
    fake_app_module.DATABASE.observer.get_workflow_run_block = AsyncMock()
    fake_app_module.DATABASE.tasks.get_task = AsyncMock()
    fake_app_module.DATABASE.tasks.update_task = AsyncMock()
    fake_app_module.DATABASE.observer.update_task_v2 = AsyncMock()
    fake_app_module.DATABASE.organizations.get_organization = AsyncMock()
    fake_app_module.DATABASE.workflows.get_workflow = AsyncMock()
    fake_app_module.DATABASE.observer.create_workflow_run_block = AsyncMock()
    fake_app_module.DATABASE.workflow_runs.update_workflow_run = AsyncMock()
    fake_app_module.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter = AsyncMock()
    fake_app_module.DATABASE.observer.update_workflow_run_block = AsyncMock()
    fake_app_module.LLM_API_HANDLER = AsyncMock()
    fake_app_module.SECONDARY_LLM_API_HANDLER = AsyncMock()
    fake_app_module.AUTO_COMPLETION_LLM_API_HANDLER = AsyncMock()
    fake_app_module.CUSTOM_SELECT_AGENT_LLM_API_HANDLER = AsyncMock()
    fake_app_module.NORMAL_SELECT_AGENT_LLM_API_HANDLER = AsyncMock()
    fake_app_module.SELECT_AGENT_LLM_API_HANDLER = AsyncMock()
    fake_app_module.SINGLE_CLICK_AGENT_LLM_API_HANDLER = AsyncMock()
    fake_app_module.SINGLE_INPUT_AGENT_LLM_API_HANDLER = AsyncMock()
    fake_app_module.EXTRACTION_LLM_API_HANDLER = AsyncMock()
    fake_app_module.CHECK_USER_GOAL_LLM_API_HANDLER = AsyncMock()
    fake_app_module.AUTO_COMPLETION_LLM_API_HANDLER = AsyncMock()
    fake_app_module.EXPERIMENTATION_PROVIDER = _LazyNamespace()
    fake_app_module.STORAGE = _LazyNamespace()

    return fake_app_module


def start_forge_stub_app() -> ForgeApp:
    force_app_instance = create_forge_stub_app()
    set_force_app_instance(force_app_instance)
    return force_app_instance
