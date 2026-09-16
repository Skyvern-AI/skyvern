from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from skyvern.config import settings
from skyvern.forge import set_force_app_instance
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.forge_app import ForgeApp
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus


def admit_block_dispatch(status: WorkflowRunStatus = WorkflowRunStatus.running) -> MagicMock:
    return MagicMock(
        side_effect=lambda workflow_run_id: nullcontext(SimpleNamespace(workflow_run_id=workflow_run_id, status=status))
    )


def create_forge_stub_app() -> ForgeApp:
    class _LazyNamespace:
        def __getattr__(self, name):
            value = AsyncMock()
            setattr(self, name, value)
            return value

    fake_app_module = ForgeApp()
    fake_app_module.DATABASE = _LazyNamespace()
    # Retry-policy-aware production paths query the attempt repository even for legacy runs.
    # Keep the shared stub's no-policy behavior explicit instead of letting _LazyNamespace
    # manufacture truthy AsyncMocks for these reads.
    fake_app_module.DATABASE.workflow_run_attempts = make_workflow_run_attempts_fake()
    fake_app_module.DATABASE.debug = SimpleNamespace(has_block_run_for_workflow_run=AsyncMock(return_value=False))
    fake_app_module.WORKFLOW_CONTEXT_MANAGER = _LazyNamespace()
    fake_app_module.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts = {}
    fake_app_module.WORKFLOW_CONTEXT_MANAGER.mask_secrets_enabled_for_run = MagicMock(return_value=False)
    fake_app_module.WORKFLOW_CONTEXT_MANAGER.secret_redaction_enabled_for_run = MagicMock(return_value=False)
    fake_app_module.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled = MagicMock(return_value=False)
    fake_app_module.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run = MagicMock(return_value=set())
    fake_app_module.WORKFLOW_CONTEXT_MANAGER.runtime_secret_values_for_artifacts = MagicMock(return_value=set())
    fake_app_module.WORKFLOW_CONTEXT_MANAGER.secret_values_for_drop_check = MagicMock(return_value=set())
    fake_app_module.WORKFLOW_CONTEXT_MANAGER.get_attempt_number = MagicMock(return_value=1)
    # Sync liveness predicate — _LazyNamespace would auto-mock it as a truthy (never-awaited) AsyncMock,
    # making every wr_ alias read as a live sharer. Default to "no run is live" so tests must opt a run
    # into liveness explicitly (non-PBS ownership signal).
    fake_app_module.WORKFLOW_CONTEXT_MANAGER.has_workflow_run_context = MagicMock(return_value=False)
    fake_app_module.WORKFLOW_SERVICE = _LazyNamespace()
    fake_app_module.BROWSER_MANAGER = _LazyNamespace()
    # get_for_task is a sync lookup returning None when no browser state is registered; _LazyNamespace
    # would otherwise auto-mock it as an (awaitable) AsyncMock, breaking sync callers.
    fake_app_module.BROWSER_MANAGER.get_for_task = MagicMock(return_value=None)
    fake_app_module.PERSISTENT_SESSIONS_MANAGER = _LazyNamespace()
    fake_app_module.ARTIFACT_MANAGER = _LazyNamespace()
    fake_app_module.AGENT_FUNCTION = _LazyNamespace()
    fake_app_module.AGENT_FUNCTION.is_block_scoped_workflow_run = AsyncMock(return_value=False)
    fake_app_module.AGENT_FUNCTION.validate_block_execution = AsyncMock()
    fake_app_module.AGENT_FUNCTION.validate_code_block = AsyncMock()
    # Secure CodeBlock runner gating — _LazyNamespace would auto-mock these as
    # truthy AsyncMocks and hijack CodeBlock.execute into the runner path. Match
    # the real OSS base no-op so unit tests exercise the legacy in-process path.
    fake_app_module.AGENT_FUNCTION.should_use_codeblock_runner = AsyncMock(return_value=False)
    fake_app_module.AGENT_FUNCTION.execute_code_block_override = AsyncMock(return_value=None)
    base_agent_function = AgentFunction()
    # Class constant, not a method — _LazyNamespace would auto-mock it into a non-iterable AsyncMock
    # and break every caller that scans it for close-page phrases.
    fake_app_module.AGENT_FUNCTION.MAGIC_LINK_CLOSE_SIGNALS = base_agent_function.MAGIC_LINK_CLOSE_SIGNALS
    # Same footgun: _LazyNamespace's truthy AsyncMock would inject extra guidance text into every
    # v3 task's extra_system_guidance join.
    fake_app_module.AGENT_FUNCTION.resolve_task_v3_extra_guidance = base_agent_function.resolve_task_v3_extra_guidance
    fake_app_module.AGENT_FUNCTION.serialize_codeblock_parameters = base_agent_function.serialize_codeblock_parameters
    fake_app_module.AGENT_FUNCTION.redact_codeblock_parameter_values = (
        base_agent_function.redact_codeblock_parameter_values
    )
    fake_app_module.AGENT_FUNCTION.prepare_codeblock_control_flow_exception = (
        base_agent_function.prepare_codeblock_control_flow_exception
    )
    # Copilot worker-dispatch gate — _LazyNamespace would auto-mock this as a truthy AsyncMock
    # and route copilot block runs down the worker-dispatch path. Match the real OSS base
    # default (False) so unit tests exercise the unavailable-worker path.
    fake_app_module.AGENT_FUNCTION.should_dispatch_copilot_block_run_to_worker = AsyncMock(return_value=False)
    # Advisory scanner hook — _LazyNamespace would auto-mock this as an AsyncMock returning a
    # non-iterable MagicMock, silently exercising the fail-open path instead of the OSS no-op.
    fake_app_module.AGENT_FUNCTION.scan_code_block_source = AsyncMock(return_value=[])
    # Sync methods — _LazyNamespace would auto-mock these as AsyncMock and break callers that use
    # the return value directly. Match the real OSS defaults.
    fake_app_module.AGENT_FUNCTION.resolve_copilot_dispatch_trigger_type = MagicMock(return_value=None)
    fake_app_module.AGENT_FUNCTION.allow_copilot_inline_code_execution = MagicMock(return_value=False)
    fake_app_module.AGENT_FUNCTION.resolve_mcp_oauth_org_lookups = MagicMock(return_value=None)
    fake_app_module.AGENT_FUNCTION.get_mcp_request_organization_id = MagicMock(return_value=None)
    # Sync method returning a key or None — _LazyNamespace would auto-mock it as a truthy
    # AsyncMock and hijack the TextPromptBlock llm_key. Match the OSS no-op.
    fake_app_module.AGENT_FUNCTION.get_fallback_llm_key = MagicMock(return_value=None)
    # Credential write-lock gating — _LazyNamespace would auto-mock these as truthy AsyncMocks,
    # forcing the update/delete credential routes down the lock path and handing `async with` a
    # coroutine instead of a context manager. Match the real OSS base no-ops (unlocked path).
    fake_app_module.AGENT_FUNCTION.should_lock_credential_write = AsyncMock(return_value=False)
    fake_app_module.AGENT_FUNCTION.credential_write_lock = MagicMock(return_value=nullcontext())
    fake_app_module.AGENT_FUNCTION.validate_credential_write = AsyncMock(return_value=None)
    fake_app_module.AGENT_FUNCTION.prepare_credential_update = AsyncMock(side_effect=lambda **kwargs: kwargs["data"])
    # Grid-collection seam — _LazyNamespace would auto-mock this as a truthy AsyncMock whose
    # awaited value is a non-None MagicMock, poisoning the extract-information prompt/cache key.
    # Match the real OSS base no-op (None → no grid rows injected).
    fake_app_module.AGENT_FUNCTION.collect_virtualized_grid_rows = AsyncMock(return_value=None)
    fake_app_module.agent = _LazyNamespace()
    fake_app_module.DATABASE.observer.update_workflow_run_block = AsyncMock()
    fake_app_module.DATABASE.observer.create_workflow_run_block = AsyncMock()
    fake_app_module.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter = AsyncMock()
    fake_app_module.DATABASE.tasks.get_last_task_for_workflow_run = AsyncMock()
    fake_app_module.DATABASE.workflow_runs.get_workflow_run = AsyncMock()
    fake_app_module.DATABASE.workflow_runs.admit_workflow_run_block_dispatch = admit_block_dispatch()
    fake_app_module.DATABASE.workflow_runs.get_secure_runner_pin = AsyncMock(return_value=None)
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
    fake_app_module.OPENAI_CLIENT = AsyncMock()
    fake_app_module.OPENAI_CUA_MODEL = settings.OPENAI_CUA_MODEL
    fake_app_module.EXPERIMENTATION_PROVIDER = _LazyNamespace()
    fake_app_module.STORAGE = _LazyNamespace()
    fake_app_module.CACHE = _LazyNamespace()

    return fake_app_module


def make_workflow_run_attempts_fake() -> SimpleNamespace:
    """Return the no-policy attempt repository used by isolated database fakes."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(rowcount=0))
    session.commit = AsyncMock()
    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=session)
    session_context.__aexit__ = AsyncMock(return_value=None)
    return SimpleNamespace(
        Session=MagicMock(return_value=session_context),
        create_attempt=AsyncMock(return_value=SimpleNamespace(attempt_number=1)),
        pin_first_attempt_browser_session_if_unset=AsyncMock(),
        get_attempts=AsyncMock(return_value=[]),
        get_interim_payload_snapshot=AsyncMock(return_value={}),
        get_latest_attempts_for_runs=AsyncMock(return_value={}),
        list_stale_pending_retries=AsyncMock(return_value=[]),
        list_attempts_needing_recovery=AsyncMock(return_value=[]),
        list_stale_dispatch_claims=AsyncMock(return_value=[]),
        release_stale_dispatch_claim=AsyncMock(return_value=False),
        mark_attempt_inputs_unrecoverable=AsyncMock(),
        refresh_attempt_finished_at=AsyncMock(return_value=None),
        fail_prepared_workflow_run=AsyncMock(return_value=False),
        claim_prepared_attempt_execution=AsyncMock(return_value=None),
        finalize_attempt=AsyncMock(return_value=SimpleNamespace(attempt_number=1)),
        revoke_or_abandon_attempt=AsyncMock(return_value=False),
        reserve_attempt_webhook_delivery=AsyncMock(return_value=None),
        claim_attempt_webhook=AsyncMock(return_value=False),
        claim_attempt_side_effects=AsyncMock(return_value=False),
        save_side_effect_progress=AsyncMock(return_value=False),
        clear_failed_interim_side_effect_lease=AsyncMock(return_value=False),
        delete_attempts_for_run=AsyncMock(),
    )


def start_forge_stub_app() -> ForgeApp:
    force_app_instance = create_forge_stub_app()
    set_force_app_instance(force_app_instance)
    return force_app_instance
