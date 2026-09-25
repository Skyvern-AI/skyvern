"""Shared builders for copilot unit tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from itertools import count
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge import app as forge_app
from skyvern.forge.sdk.api.llm import api_handler_factory
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.copilot import agent as copilot_agent
from skyvern.forge.sdk.copilot import runtime as copilot_runtime
from skyvern.forge.sdk.copilot.active_run_session import ActiveRunSessionAssociation
from skyvern.forge.sdk.copilot.agent import run_copilot_agent
from skyvern.forge.sdk.copilot.browser_ablation import CopilotEvalMode
from skyvern.forge.sdk.copilot.build_test_outcome import RecordedBuildTestOutcome
from skyvern.forge.sdk.copilot.context import AgentResult, CopilotContext
from skyvern.forge.sdk.copilot.diagnosis_repair_contract import (
    DiagnosisInput,
    DiagnosisRepairContract,
    DiagnosisResult,
    RepairDecision,
    RepairNextAction,
    VerificationResult,
)
from skyvern.forge.sdk.copilot.enforcement import CopilotTotalTimeoutError, _mark_copilot_total_timeout
from skyvern.forge.sdk.copilot.repair_origin_run import RepairOriginBinding
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion
from skyvern.forge.sdk.copilot.runtime import record_sensitive_origin_run_taint, register_sensitive_origin_run_lease
from skyvern.forge.sdk.copilot.tools import run_execution as run_execution_module
from skyvern.forge.sdk.copilot.tools import scouting as scouting_module
from skyvern.forge.sdk.copilot.turn_origin import TurnOrigin
from skyvern.forge.sdk.copilot.workflow_yaml import _process_workflow_yaml as process_workflow_yaml
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.enums import WorkflowRunTriggerType
from skyvern.forge.sdk.schemas.credentials import Credential, CredentialType, CredentialVaultType, PasswordCredential
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatRequest, WorkflowCopilotTitleUpdate
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, WorkflowParameter, WorkflowParameterType
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunParameter, WorkflowRunStatus
from skyvern.schemas.proxy_location import ProxyLocationInput
from skyvern.schemas.runs import ProxyLocation
from skyvern.schemas.workflows import BlockType
from skyvern.services import workflow_service as workflow_service_module
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import ActionStatus

DISPATCHED_LOGIN_GATE_HTML = (
    "<html><head><title>Sign in</title></head><body><main>"
    "<h1>Sign in to continue</h1>"
    '<form id="signin" action="/session" method="post">'
    '<label for="account-email">Email</label>'
    '<input id="account-email" name="email" type="email" required />'
    '<label for="account-password">Password</label>'
    '<input id="account-password" name="password" type="password" required />'
    '<button type="submit">Sign in</button>'
    "</form></main></body></html>"
)
DISPATCHED_RESULTS_HTML = (
    "<html><head><title>Available providers</title></head><body><main>"
    "<h1>Available providers</h1>"
    '<table id="provider-results"><tbody>'
    "<tr><td>Example Fiber</td><td>up to 500 Mbps</td></tr>"
    "<tr><td>Example Cable</td><td>up to 300 Mbps</td></tr>"
    "</tbody></table></main></body></html>"
)
DISPATCHED_NAV_ONLY_HTML = (
    "<html><head><title>Site map</title></head><body><main>"
    '<a href="https://example.test/plans">Plans</a>'
    '<a href="https://example.test/support">Support</a>'
    "</main></body></html>"
)


def wire_credential_vault(
    monkeypatch: pytest.MonkeyPatch,
    secrets: PasswordCredential,
    *,
    credential_id: str = "cred_1",
    name: str = "authtest simple",
) -> Credential:
    """Serve one saved credential from the org database and ``secrets`` from its vault."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    credential = Credential(
        credential_id=credential_id,
        organization_id="org-1",
        name=name,
        vault_type=CredentialVaultType.SKYVERN,
        item_id="item_1",
        credential_type=CredentialType.PASSWORD,
        username=secrets.username,
        card_last4=None,
        card_brand=None,
        created_at=now,
        modified_at=now,
    )
    monkeypatch.setattr(
        forge_app.DATABASE,
        "credentials",
        SimpleNamespace(
            get_credential=AsyncMock(return_value=credential),
            get_credentials_by_ids=AsyncMock(return_value=[credential]),
        ),
        raising=False,
    )
    vault = SimpleNamespace(get_credential_item=AsyncMock(return_value=SimpleNamespace(name=name, credential=secrets)))
    # `app` is an AppHolder proxy without __delattr__; patch the underlying instance so teardown can delete it.
    monkeypatch.setattr(
        object.__getattribute__(forge_app, "_inst"),
        "CREDENTIAL_VAULT_SERVICES",
        {CredentialVaultType.SKYVERN: vault},
        raising=False,
    )
    return credential


def make_stub_artifact(
    artifact_id: str,
    file_name: str,
    file_size: int | None,
    artifact_type: ArtifactType = ArtifactType.DOWNLOAD,
) -> SimpleNamespace:
    return SimpleNamespace(
        artifact_id=artifact_id,
        uri=f"s3://bucket/{file_name}",
        file_size=file_size,
        artifact_type=artifact_type,
    )


def make_stub_html_artifact(
    artifact_id: str,
    artifact_type: ArtifactType,
    file_size: int | None = 400,
    created_at: datetime | None = None,
) -> SimpleNamespace:
    artifact = make_stub_artifact(artifact_id, f"{artifact_id}.html", file_size, artifact_type=artifact_type)
    artifact.created_at = created_at or datetime(2026, 7, 9, tzinfo=timezone.utc)
    return artifact


def stub_artifact_app(
    monkeypatch: pytest.MonkeyPatch,
    artifacts: list[SimpleNamespace],
    retrieved: dict[str, bytes],
    *,
    by_ids: list[SimpleNamespace] | None = None,
) -> list[str]:
    retrieved_ids: list[str] = []

    async def fake_get_artifacts_for_run(
        run_id: str, *, organization_id: str, artifact_types: object
    ) -> list[SimpleNamespace]:
        return artifacts

    async def fake_get_artifacts_by_ids(artifact_ids: list[str], *, organization_id: str) -> list[SimpleNamespace]:
        pool = {artifact.artifact_id: artifact for artifact in (by_ids if by_ids is not None else artifacts)}
        return [pool[artifact_id] for artifact_id in artifact_ids if artifact_id in pool]

    async def fake_retrieve_artifact(artifact: SimpleNamespace) -> bytes:
        retrieved_ids.append(artifact.artifact_id)
        return retrieved.get(artifact.artifact_id, b"")

    fake_app = SimpleNamespace(
        DATABASE=SimpleNamespace(
            artifacts=SimpleNamespace(
                get_artifacts_for_run=fake_get_artifacts_for_run,
                get_artifacts_by_ids=fake_get_artifacts_by_ids,
            )
        ),
        ARTIFACT_MANAGER=SimpleNamespace(retrieve_artifact=fake_retrieve_artifact),
    )
    monkeypatch.setattr(run_execution_module, "app", fake_app)
    return retrieved_ids


_RUN_SESSION_ID = "pbs_run"
_CHAT_SESSION_ID = "pbs_chat"
# The chat session answers with a different proxy than the run session, so a lookup that reads the
# wrong session shows up as the wrong label instead of passing.
_CHAT_SESSION_PROXY_LOCATION = ProxyLocation.RESIDENTIAL_ZA


def _fake_workflow_run(status: str) -> SimpleNamespace:
    return SimpleNamespace(
        status=WorkflowRunStatus(status),
        created_at=datetime(2026, 4, 21, 11, 0, 0),
        modified_at=datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc),
        trigger_type=None,
        browser_session_id=None,
        failure_reason=None,
    )


HARNESS_RUN_CREATED_AT = datetime(2026, 4, 21, 12, 0, 0)


def harness_run(
    workflow_run_id: str,
    *,
    created_at: datetime = HARNESS_RUN_CREATED_AT,
    status: str = "completed",
    trigger_type: WorkflowRunTriggerType | None = None,
    copilot_session_id: str | None = None,
    workflow_permanent_id: str = "wpid-1",
    organization_id: str = "org-1",
) -> SimpleNamespace:
    """Naive-UTC created_at, as the database returns it."""
    return SimpleNamespace(
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
        workflow_permanent_id=workflow_permanent_id,
        workflow_id="wf-1",
        status=status,
        created_at=created_at,
        trigger_type=trigger_type,
        copilot_session_id=copilot_session_id,
        failure_reason=None,
        browser_session_id="pbs-1",
    )


def install_get_run_results_harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    blocks: list[MagicMock],
    run_status: str = "failed",
    dispatch_to_worker: bool = True,
    workflow_parameters: list[dict[str, str]] | None = None,
    attach_action_traces: Callable[..., Awaitable[None]] | None = None,
    recent_actions: list[MagicMock] | None = None,
    attach_failed_block_screenshots: Callable[..., Awaitable[None]] | None = None,
    other_runs: list[SimpleNamespace] | None = None,
    carried_successful_run_id: str | None = None,
    carried_run_id: str | None = None,
) -> SimpleNamespace:
    """Stub the collaborators ``_get_run_results`` reaches and return the ctx to call it with; the run pool is
    ``wr-1`` plus ``other_runs``, and run lookup and history listing honor their arguments."""
    pool = [harness_run("wr-1", status=run_status), *(other_runs or [])]
    workflow = SimpleNamespace(workflow_definition=SimpleNamespace(parameters=workflow_parameters or []))

    async def get_workflow_run(workflow_run_id: str, organization_id: str | None = None) -> SimpleNamespace | None:
        return next(
            (r for r in pool if r.workflow_run_id == workflow_run_id and r.organization_id == organization_id),
            None,
        )

    async def list_runs(
        *,
        workflow_permanent_id: str,
        organization_id: str,
        page: int = 1,
        page_size: int = 10,
        status: list[WorkflowRunStatus] | None = None,
        created_at_start: datetime | None = None,
    ) -> list[SimpleNamespace]:
        matching = [
            r
            for r in pool
            if r.workflow_permanent_id == workflow_permanent_id
            and r.organization_id == organization_id
            and r.copilot_session_id is None
            and (not status or r.status in status)
            and (created_at_start is None or r.created_at >= created_at_start)
        ]
        matching.sort(key=lambda r: r.created_at, reverse=True)
        return matching[(page - 1) * page_size : page * page_size]

    class _AppStub:
        class DATABASE:
            workflow_runs = SimpleNamespace(
                get_workflow_run=get_workflow_run,
                get_workflow_runs_for_workflow_permanent_id=list_runs,
            )

            class workflows:
                get_workflow = AsyncMock(return_value=None)
                get_workflow_for_workflow_run = AsyncMock(return_value=workflow)

            class observer:
                get_workflow_run_blocks = AsyncMock(return_value=blocks)

            class tasks:
                get_recent_actions_for_tasks = AsyncMock(return_value=list(recent_actions or []))

        class AGENT_FUNCTION:
            should_dispatch_copilot_block_run_to_worker = AsyncMock(return_value=dispatch_to_worker)

        WORKFLOW_SERVICE = SimpleNamespace(get_workflow_runs_for_workflow_permanent_id=list_runs)

    monkeypatch.setattr(run_execution_module, "app", _AppStub())
    if attach_action_traces is not None:
        monkeypatch.setattr(run_execution_module, "_attach_action_traces", attach_action_traces)
    # Stubbed by default so most callers need no artifact store; pass the real function to assert
    # that at-failure evidence actually reaches the packet rather than that the call was made.
    monkeypatch.setattr(
        run_execution_module,
        "_attach_failed_block_screenshots",
        attach_failed_block_screenshots or AsyncMock(),
    )
    monkeypatch.setattr(run_execution_module, "_attach_registered_output_parameter_values", AsyncMock(return_value={}))
    monkeypatch.setattr(run_execution_module, "_fetch_dispatched_terminal_page_evidence", AsyncMock(return_value=None))
    return SimpleNamespace(
        organization_id="org-1",
        workflow_permanent_id="wpid-1",
        copilot_total_timeout_exceeded=False,
        last_successful_run_blocks_workflow_run_id=carried_successful_run_id,
        last_run_blocks_workflow_run_id=carried_run_id,
        proposal_workflow_run_id=None,
        dispatched_run_ids_this_turn=set(),
    )


def run_result_action_row(
    task_id: str,
    action_type: ActionType,
    status: ActionStatus,
    *,
    code_line: int | None = None,
) -> MagicMock:
    action = MagicMock()
    action.task_id = task_id
    action.step_id = f"stp_{task_id}"
    action.action_type = action_type
    action.status = status
    action.reasoning = None
    action.element_id = None
    action.response = None
    action.output = {"code_line": code_line} if code_line is not None else None
    return action


def run_result_block_row(
    label: str,
    status: str,
    final_url: str | None = None,
    *,
    failure_reason: str | None = None,
    error_codes: list[str] | None = None,
    task_id: str | None = None,
) -> MagicMock:
    row = MagicMock()
    row.label = label
    row.block_type = SimpleNamespace(name="code")
    row.status = status
    row.failure_reason = failure_reason
    row.error_codes = error_codes or []
    row.output = None
    row.task_id = task_id
    row.final_url = final_url
    row.workflow_run_block_id = f"wrb_{label}"
    return row


async def install_run_blocks_harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    workflow_yaml: str,
    polled_status: str,
    dispatch_to_worker: bool = False,
    terminal_blocks: list[WorkflowRunBlock] | None = None,
    recent_actions: list[MagicMock] | None = None,
    run_proxy_location: ProxyLocationInput = None,
    run_session_proxy_location: ProxyLocationInput = None,
) -> dict[str, Any]:
    """Stub the collaborators an inline ``_run_blocks_and_collect_debug`` call reaches, with the
    polled run parked on ``polled_status`` so the watchdog decides the exit."""
    monkeypatch.setattr(forge_app.WORKFLOW_SERVICE, "get_workflow_by_permanent_id", AsyncMock(return_value=None))
    workflow = await process_workflow_yaml(
        settings_fallback_yaml="enable_self_healing: false",
        workflow_id="w_source",
        workflow_permanent_id="wfp-1",
        organization_id="org-1",
        workflow_yaml=workflow_yaml,
    )
    now = datetime.now(timezone.utc)
    organization = Organization(
        organization_id="org-1",
        organization_name="Test Org",
        created_at=now,
        modified_at=now,
    )
    captured: dict[str, Any] = {"workflow": workflow, "executor_cancelled": False}

    database = MagicMock()
    database.workflows.get_workflow_by_permanent_id = AsyncMock(return_value=workflow)
    database.workflows.get_workflow = AsyncMock(return_value=workflow)
    database.organizations.get_organization = AsyncMock(return_value=organization)
    persisted_output_params = [p for p in workflow.workflow_definition.parameters if isinstance(p, OutputParameter)]
    persisted_workflow_params = [p for p in workflow.workflow_definition.parameters if isinstance(p, WorkflowParameter)]
    database.workflow_params.get_workflow_output_parameters = AsyncMock(return_value=persisted_output_params)
    database.observer.get_workflow_run_blocks = AsyncMock(return_value=terminal_blocks or [])
    database.tasks.get_recent_actions_for_tasks = AsyncMock(return_value=list(recent_actions or []))
    database.workflow_runs.get_workflow_run = AsyncMock(return_value=_fake_workflow_run(status=polled_status))
    database.workflow_runs.get_workflow_runs_for_workflow_permanent_id = AsyncMock(return_value=[])
    monkeypatch.setattr(forge_app, "DATABASE", database)

    async def _execute_workflow(**_kwargs: Any) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            captured["executor_cancelled"] = True
            raise

    workflow_service = MagicMock()
    workflow_service.get_workflow_parameters = AsyncMock(return_value=persisted_workflow_params)
    workflow_service.execute_workflow = AsyncMock(side_effect=_execute_workflow)
    workflow_service.create_copilot_dispatch_draft_version = AsyncMock(return_value=workflow)
    monkeypatch.setattr(forge_app, "WORKFLOW_SERVICE", workflow_service)
    monkeypatch.setattr(
        forge_app.AGENT_FUNCTION,
        "should_dispatch_copilot_block_run_to_worker",
        AsyncMock(return_value=dispatch_to_worker),
    )
    monkeypatch.setattr(
        forge_app.AGENT_FUNCTION,
        "allow_copilot_inline_code_execution",
        MagicMock(return_value=False),
    )

    workflow_run = SimpleNamespace(
        workflow_run_id="wr_paused",
        workflow_id="w_source",
        sequential_credential_id=None,
        proxy_location=run_proxy_location,
        runnable_id=None,
    )
    monkeypatch.setattr(workflow_service_module, "prepare_workflow", AsyncMock(return_value=workflow_run))

    async def _get_session(session_id: str, _organization_id: str | None = None) -> SimpleNamespace:
        proxy_location = run_session_proxy_location if session_id == _RUN_SESSION_ID else _CHAT_SESSION_PROXY_LOCATION
        return SimpleNamespace(proxy_location=proxy_location, runnable_id=None)

    monkeypatch.setattr(forge_app.PERSISTENT_SESSIONS_MANAGER, "get_session", _get_session)

    polled_run = _fake_workflow_run(status=polled_status)

    async def _read_progress(_ctx: CopilotContext, _run_id: str) -> tuple[Any, Any, Any]:
        return polled_run, now, now

    monkeypatch.setattr(run_execution_module, "_read_progress_sources", _read_progress)
    monkeypatch.setattr(run_execution_module, "RUN_BLOCKS_POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(run_execution_module, "_fallback_page_info", AsyncMock(return_value=("", "")))

    association = ActiveRunSessionAssociation(
        organization_id="org-1",
        workflow_permanent_id="wfp-1",
        debug_browser_session_id=_CHAT_SESSION_ID,
        run_browser_session_id=_RUN_SESSION_ID,
        workflow_run_id="wr_paused",
        turn_id="turn-1",
        generation="gen-1",
        expires_at=now + timedelta(minutes=5),
    )
    captured["publish"] = AsyncMock(return_value=association)
    captured["clear"] = AsyncMock(return_value=True)
    captured["cancel_run_task"] = AsyncMock(return_value=None)
    captured["cooperative_cancel"] = AsyncMock(return_value=None)
    monkeypatch.setattr(run_execution_module, "publish_active_run_session", captured["publish"])
    monkeypatch.setattr(run_execution_module, "clear_active_run_session", captured["clear"])
    monkeypatch.setattr(run_execution_module, "_cancel_run_task_if_not_final", captured["cancel_run_task"])
    monkeypatch.setattr(run_execution_module, "_cooperative_cancel_dispatched_run", captured["cooperative_cancel"])
    if dispatch_to_worker:
        captured["worker_execute"] = AsyncMock(return_value=None)
        monkeypatch.setattr(
            run_execution_module.AsyncExecutorFactory,
            "get_executor",
            MagicMock(return_value=SimpleNamespace(execute_workflow=captured["worker_execute"])),
        )
        monkeypatch.setattr(run_execution_module, "_delete_dispatch_draft_if_run_final", AsyncMock(return_value=None))
        monkeypatch.setattr(
            run_execution_module, "_capture_dispatched_terminal_page_evidence", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            run_execution_module, "_attach_registered_output_parameter_values", AsyncMock(return_value={})
        )
    return captured


HANDBACK_WORKFLOW_YAML = """
title: extraction example
workflow_definition:
  parameters: []
  blocks:
    - block_type: extraction
      label: extract_heading
      url: https://example.com
      data_extraction_goal: Extract the page heading.
"""


def terminal_extraction_block(
    status: str,
    *,
    final_url: str | None = None,
    label: str = "extract_heading",
    failure_reason: str | None = None,
    task_id: str | None = None,
) -> WorkflowRunBlock:
    return WorkflowRunBlock(
        label=label,
        block_type=BlockType.EXTRACTION,
        status=status,
        final_url=final_url,
        task_id=task_id,
        failure_reason=(
            (failure_reason or 'Timeout exceeded: waiting for locator("#heading") to be visible')
            if status == "failed"
            else None
        ),
        workflow_run_block_id=f"wrb_{label}",
        workflow_run_id="wr_paused",
        organization_id="org-1",
        created_at=datetime(2026, 4, 21, 12, 5, tzinfo=UTC),
        modified_at=datetime(2026, 4, 21, 12, 5, tzinfo=UTC),
    )


SEARCH_THEN_SELECT_WORKFLOW_YAML = """
title: search then select
workflow_definition:
  parameters: []
  blocks:
    - block_type: extraction
      label: run_search
      url: https://fixture.test
      data_extraction_goal: Extract the search results.
    - block_type: extraction
      label: select_first_result
      data_extraction_goal: Extract the selected result.
"""


def page_only_failed_block() -> WorkflowRunBlock:
    """A failed block with no failure_reason, so the post-run page is its only structural signal."""
    return WorkflowRunBlock(
        label="extract_heading",
        block_type=BlockType.EXTRACTION,
        status="failed",
        failure_reason=None,
        workflow_run_block_id="wrb_extract_heading",
        workflow_run_id="wr_paused",
        organization_id="org-1",
        created_at=datetime(2026, 4, 21, 12, 5, tzinfo=UTC),
        modified_at=datetime(2026, 4, 21, 12, 5, tzinfo=UTC),
    )


def same_run_page_evidence() -> dict[str, object]:
    return {
        "observed_after_workflow_run": True,
        "workflow_run_id": "wr_paused",
        "source_browser_session_id": "pbs_run",
        "current_url": "https://example.com/done",
        "page_title": "Done",
        "inspected_url": "https://example.com/done",
    }


def count_record_and_send(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    counts = {"record": 0, "send": 0}
    real_record = run_execution_module.record_build_test_outcome
    real_send = run_execution_module._send_run_outcome_update

    def _record(ctx: object, outcome: object) -> None:
        counts["record"] += 1
        real_record(ctx, outcome)

    async def _send(*args: object, **kwargs: object) -> None:
        counts["send"] += 1
        await real_send(*args, **kwargs)

    monkeypatch.setattr(run_execution_module, "record_build_test_outcome", _record)
    monkeypatch.setattr(run_execution_module, "_send_run_outcome_update", _send)
    return counts


async def handback_ctx(
    monkeypatch: pytest.MonkeyPatch,
    *,
    polled_status: str,
    block_status: str,
    terminal_blocks: list[WorkflowRunBlock] | None = None,
) -> CopilotContext:
    harness = await install_run_blocks_harness(
        monkeypatch,
        workflow_yaml=HANDBACK_WORKFLOW_YAML,
        polled_status=polled_status,
        terminal_blocks=terminal_blocks or [terminal_extraction_block(block_status)],
    )
    ctx = make_copilot_ctx(browser_session_id="pbs_chat")
    ctx.staged_workflow = harness["workflow"]
    ctx.frontier_resume_session_id = "pbs_run"
    return ctx


class FakeTab:
    def __init__(self, url: str) -> None:
        self.url = url
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed


class FakeTabbedBrowserState:
    """A browser whose tab list and selected tab the test moves between calls: navigate by assigning a
    tab's ``url``, close one with ``close``, select one by assigning ``active``."""

    def __init__(self, *urls: str, active: int = 0) -> None:
        self.tabs = [FakeTab(url) for url in urls]
        self.active: FakeTab | None = self.tabs[active] if self.tabs else None
        self.browser_context = SimpleNamespace(pages=self.tabs)

    def close(self, tab: FakeTab) -> None:
        tab.closed = True
        if self.active is tab:
            self.active = next((open_tab for open_tab in self.tabs if not open_tab.closed), None)

    async def get_working_page(self, *, prune_excess_pages: bool = True) -> FakeTab | None:
        return self.active

    async def get_or_create_page(self) -> FakeTab | None:
        return self.active

    async def list_valid_pages(self, max_pages: int = 0) -> list[FakeTab]:
        return [tab for tab in self.tabs if not tab.closed]


def patch_browser_tabs(
    monkeypatch: pytest.MonkeyPatch, states: FakeTabbedBrowserState | dict[str, FakeTabbedBrowserState] | None
) -> None:
    """Resolve browser sessions to the given tabbed state (one for every session, or one per session id);
    None resolves to no browser at all, which the taint guard treats as unreadable."""

    async def resolve(_ctx: object, *, session_id: str | None = None) -> FakeTabbedBrowserState | None:
        if isinstance(states, dict):
            return states.get(session_id or "")
        return states

    monkeypatch.setattr(copilot_runtime, "resolve_browser_state_for_context", resolve)
    monkeypatch.setattr(scouting_module, "resolve_browser_state_for_context", resolve)


def patch_browser_tab_count(monkeypatch: pytest.MonkeyPatch, open_tabs: int | None) -> None:
    patch_browser_tabs(
        monkeypatch,
        None if open_tabs is None else FakeTabbedBrowserState(*(["https://tab.example.test/"] * open_tabs)),
    )


def origin_run_input(
    key: str,
    value: bool | int | float | str | dict | list,
    ptype: WorkflowParameterType = WorkflowParameterType.FILE_URL,
    *,
    default_value: str | None = None,
) -> tuple[WorkflowParameter, WorkflowRunParameter]:
    now = datetime.now(UTC)
    parameter = WorkflowParameter(
        workflow_parameter_id=f"wp_origin_{key}",
        workflow_parameter_type=ptype,
        key=key,
        description=None,
        workflow_id="wf_origin",
        default_value=default_value,
        created_at=now,
        modified_at=now,
    )
    return parameter, WorkflowRunParameter(
        workflow_run_id="wr_origin",
        workflow_parameter_id=parameter.workflow_parameter_id,
        value=value,
        created_at=now,
    )


def make_copilot_ctx(**overrides: object) -> CopilotContext:
    defaults: dict[str, object] = dict(
        organization_id="org-1",
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_yaml="",
        browser_session_id=None,
        stream=MagicMock(),
    )
    defaults.update(overrides)
    return CopilotContext(**defaults)


def make_verified_goal_contract(
    *, next_action: RepairNextAction = RepairNextAction.NO_CHANGE
) -> DiagnosisRepairContract:
    return DiagnosisRepairContract(
        diagnosis_input=DiagnosisInput(source_tool="update_and_run_blocks"),
        diagnosis_result=DiagnosisResult(),
        repair_decision=RepairDecision(next_action=next_action),
        verification_result=VerificationResult(
            user_goal_satisfied=True,
            completion_contract_satisfied=True,
        ),
    )


def make_completion_criterion(
    cid: str,
    outcome: str,
    *,
    level: str = "run",
    method_mandated: bool = False,
    output_path: str | None = None,
    contingent_on: str | None = None,
    contingent_antecedent_output_path: str | None = None,
    antecedent_family: str | None = None,
    kind: str = "outcome",
    terminal_action_family: str | None = None,
    deliverable_kind: str | None = None,
    deliverable_confirmation_criterion_id: str | None = None,
    expected_output_value: str | None = None,
    expected_output_shape: str | None = None,
    requested_output_evidence_source: str = "runtime_output",
    requested_output_path_mint_source: str | None = None,
    classification_output_key: str | None = None,
    expected_classification: str | bool | None = None,
    requested_output_corroborator: bool = False,
    mint_degrade: str | None = None,
    requested_output_floor_rekeyed: bool = False,
    floor_rekeyed_from_path: str | None = None,
) -> CompletionCriterion:
    return CompletionCriterion(
        id=cid,
        outcome=outcome,
        level=level,  # type: ignore[arg-type]
        method_mandated=method_mandated,
        output_path=output_path,
        contingent_on=contingent_on,
        contingent_antecedent_output_path=contingent_antecedent_output_path,
        antecedent_family=antecedent_family,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        terminal_action_family=terminal_action_family,  # type: ignore[arg-type]
        deliverable_kind=deliverable_kind,  # type: ignore[arg-type]
        deliverable_confirmation_criterion_id=deliverable_confirmation_criterion_id,
        expected_output_value=expected_output_value,
        expected_output_shape=expected_output_shape,  # type: ignore[arg-type]
        requested_output_evidence_source=requested_output_evidence_source,  # type: ignore[arg-type]
        requested_output_path_mint_source=requested_output_path_mint_source,  # type: ignore[arg-type]
        classification_output_key=classification_output_key,
        expected_classification=expected_classification,
        requested_output_corroborator=requested_output_corroborator,
        mint_degrade=mint_degrade,  # type: ignore[arg-type]
        requested_output_floor_rekeyed=requested_output_floor_rekeyed,
        floor_rekeyed_from_path=floor_rekeyed_from_path,
    )


def two_page_login_yaml(*, submit_selector: str = "Login") -> str:
    """The shape copilot emits in code-block mode: branch bodies are code inside one always-executed
    block, so a passing run can traverse it without reaching the guarded call."""
    return f"""
    title: Sign in and read the metric
    workflow_definition:
      blocks:
      - block_type: code
        label: sign_in_and_read
        code: |
          await page.fill("#user", "demo")
          await page.click("#submit")
          if await page.locator("#token").count():
              await page.get_by_role("button", name="{submit_selector}", exact=True).click()
          return {{"visitors": "9.42K"}}
    """


def straight_line_login_yaml() -> str:
    """One always-executed code block with no branching: executing it reaches every call in it."""
    return """
    title: Sign in and read the metric
    workflow_definition:
      blocks:
      - block_type: code
        label: sign_in_and_read
        code: |
          await page.fill("#user", "demo")
          await page.get_by_role("button", name="Login", exact=True).click()
          return {"visitors": "9.42K"}
    """


def failed_second_factor_run(run_id: str) -> RecordedBuildTestOutcome:
    return RecordedBuildTestOutcome(
        phase="persisted_block_run",
        attempted_tool="update_and_run_blocks",
        attempted_block_label="sign_in_and_read",
        attempted_call_ref="role:button:Login",
        verdict="repairable_failure",
        reason_code="runtime_block_failure",
        workflow_run_id=run_id,
        block_labels=["sign_in_and_read"],
        structural_failure_identity="locator-timeout-identity",
    )


def passing_run(run_id: str, block_labels: list[str]) -> RecordedBuildTestOutcome:
    return RecordedBuildTestOutcome(
        phase="persisted_block_run",
        attempted_tool="update_and_run_blocks",
        verdict="progress_observed",
        reason_code="run_completed_unevaluated",
        workflow_run_id=run_id,
        block_labels=block_labels,
        structural_failure_identity="",
        evidence_refs=["rows:1"],
    )


InteractionFieldValue = str | int | bool | None | list[Any] | dict[str, Any]


def carried_interaction(**fields: InteractionFieldValue) -> dict[str, Any]:
    """One entry of the cross-turn carried trajectory.

    The record is plain interaction dicts, so this only spares tests the brace noise.
    """
    return dict(fields)


def make_model_input_data(items: list[Any], *, instructions: str | None = None, context: Any = None) -> Any:
    """Build a fake CallModelData payload with a model_data.input list.

    ``CallModelData.context`` is the run context itself (``TContext | None``), not a wrapper around
    one; a fake that nests it hides an attribute error behind a passing test.
    """
    return SimpleNamespace(
        model_data=SimpleNamespace(input=list(items), instructions=instructions),
        context=context,
    )


class FakeMCPServerManager:
    def __init__(self, servers: object) -> None:
        self.active_servers = servers

    async def __aenter__(self) -> FakeMCPServerManager:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


def stub_copilot_agent_loop(
    monkeypatch: pytest.MonkeyPatch, run_with_enforcement: Callable[..., Awaitable[object]]
) -> None:
    def fake_resolve_model_config(
        _handler: object, *, copilot_config: object = None, llm_key_override: str | None = None
    ) -> tuple[str, object, str, bool]:
        return f"model-{llm_key_override or 'PRIMARY'}", object(), llm_key_override or "PRIMARY", True

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.agent._resolve_live_browser_session_id", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("agents.mcp.MCPServerManager", FakeMCPServerManager)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.model_resolver.resolve_model_config", fake_resolve_model_config)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.run_with_enforcement", run_with_enforcement)
    monkeypatch.setattr(copilot_agent, "schedule_agent_naming", lambda *_args: None)


SENSITIVE_DISCLOSURE_WITHHOLDING_ARMS = [
    "registry_missing",
    "registry_incomplete",
    "registry_other_run",
    "run_still_active",
    "run_id_unclaimed",
    "second_browser_run_replaced_registry",
    "same_session_earlier_run_unbound",
]


def taint_by_terminal_run(ctx: Any, *, workflow_run_id: str, session_id: str) -> None:
    """Mark ``session_id`` as the page a finished credential run left, attributed to that run."""
    record_sensitive_origin_run_taint(ctx, workflow_run_id=workflow_run_id, session_id=session_id)


def remove_sensitive_disclosure_prerequisite(ctx: Any, arm: str) -> None:
    """Drop exactly one prerequisite of the terminal-matching-registry disclosure route."""
    registry = ctx.origin_run_redaction_registry
    if arm == "registry_missing":
        ctx.origin_run_redaction_registry = None
    elif arm == "registry_incomplete":
        ctx.origin_run_redaction_registry = replace(registry, contains_all_sensitive_values=False)
    elif arm == "registry_other_run":
        ctx.origin_run_redaction_registry = replace(registry, workflow_run_id="wr_unrelated")
    elif arm == "run_still_active":
        register_sensitive_origin_run_lease(
            ctx, workflow_run_id=registry.workflow_run_id, session_id=ctx.browser_session_id
        )
    elif arm == "run_id_unclaimed":
        ctx.last_run_blocks_workflow_run_id = None
    elif arm == "second_browser_run_replaced_registry":
        # A later run on another browser finished with a complete registry and is the run the
        # model now claims; the page under inspection was tainted by the earlier run, whose
        # values were never bound. The complete registry must not unlock that page.
        record_sensitive_origin_run_taint(ctx, workflow_run_id="wr_second", session_id="pbs_second_browser")
        ctx.last_run_blocks_workflow_run_id = "wr_second"
        ctx.origin_run_redaction_registry = replace(
            registry, workflow_run_id="wr_second", contains_all_sensitive_values=True
        )
    elif arm == "same_session_earlier_run_unbound":
        # An earlier run on this same page ended without completing its registry, so a value
        # it entered may be on the page while the claimed run's complete registry knows nothing of it.
        record_sensitive_origin_run_taint(ctx, workflow_run_id="wr_earlier", session_id=ctx.browser_session_id)
    else:
        raise AssertionError(f"unknown arm {arm}")


TURN_EXIT_PATHS = ("normal", "model_error", "deadline", "cancel")


async def run_turn_to_exit(
    monkeypatch: pytest.MonkeyPatch,
    *,
    manager: MagicMock,
    exit_path: str,
    session_id: str | None = "pbs_turn",
    browser_state: MagicMock | None = None,
    resolve_session_id: str | None = None,
    eval_mode: CopilotEvalMode | None = None,
    turn_origin: TurnOrigin = TurnOrigin.interactive,
    attach_through: str = "resolve",
    on_attached: Callable[[CopilotContext], Awaitable[None]] | None = None,
    get_browser_state: AsyncMock | None = None,
) -> tuple[AgentResult | None, BaseException | None]:
    """Drive one real ``run_copilot_agent`` turn to ``exit_path`` against ``manager``, attaching
    through the real resolve funnel so the finalizer releases only what that funnel recorded."""
    manager.get_browser_state = get_browser_state or AsyncMock(return_value=browser_state)
    if turn_origin == TurnOrigin.code_block_ai_fallback and browser_state is not None:
        browser_state.get_working_page = AsyncMock(return_value=MagicMock())

    async def _exit(ctx: CopilotContext) -> None:
        if on_attached is not None:
            await on_attached(ctx)
        if exit_path == "model_error":
            raise RuntimeError("model stream failed")
        if exit_path == "deadline":
            _mark_copilot_total_timeout(ctx, elapsed_seconds=901.0, iteration=1)
            raise CopilotTotalTimeoutError()
        if exit_path == "cancel":
            raise asyncio.CancelledError()

    async def fake_turn(**kwargs: Any) -> SimpleNamespace:
        ctx = kwargs["ctx"]
        ctx.browser_session_id = session_id
        ctx.turn_origin = turn_origin
        ctx.injected_browser_state = browser_state
        ctx.heal_workflow_run_id = "wr_heal"
        if attach_through == "liveness_probe":
            assert session_id is not None
            await copilot_agent._registered_browser_state_liveness(session_id, ctx.organization_id, ctx)
        elif attach_through == "resolve":
            await copilot_runtime.resolve_browser_state_for_context(ctx, session_id=resolve_session_id)
        await _exit(ctx)
        return SimpleNamespace(final_output=json.dumps({"type": "REPLY", "user_response": "ok"}), new_items=[])

    stub_copilot_agent_loop(monkeypatch, fake_turn)
    workflow_run_id: str | None = None
    if attach_through == "prior_run_hydration":
        assert session_id is not None
        workflow_run_id = "wr_origin"
        origin = RepairOriginBinding(
            workflow_run_id=workflow_run_id,
            browser_session_id=session_id,
            refusal=None,
            status=WorkflowRunStatus.failed,
        )
        monkeypatch.setattr(copilot_agent, "seed_repair_origin_run", AsyncMock(return_value=origin))

        async def hydrate_and_exit(ctx: CopilotContext, *, workflow_run_id: str | None) -> None:
            await copilot_runtime.resolve_browser_state_for_context(ctx, session_id=session_id)
            await _exit(ctx)

        monkeypatch.setattr(copilot_agent, "hydrate_prior_run_packet", hydrate_and_exit)
    mock_app = MagicMock()
    mock_app.PERSISTENT_SESSIONS_MANAGER = manager
    monkeypatch.setattr(copilot_runtime, "app", mock_app)
    monkeypatch.setattr(copilot_agent, "_manager_can_probe_registered_browser_state", lambda: True)

    try:
        result = await run_copilot_agent(
            stream=MagicMock(),
            organization_id="org-1",
            chat_request=WorkflowCopilotChatRequest(
                workflow_permanent_id="wfp-1",
                workflow_id="wf-1",
                workflow_copilot_chat_id="chat-1",
                message="read the page title and tell me what it is",
                workflow_yaml="",
                workflow_run_id=workflow_run_id,
            ),
            chat_history=[],
            global_llm_context=None,
            llm_api_handler=SimpleNamespace(llm_key="PRIMARY"),
            raw_secret_safety_handler=AsyncMock(
                return_value={"version": "1", "state": "clean", "handling": "none", "citations": []}
            ),
            api_key="sk-test",
            eval_mode=eval_mode,
        )
    except BaseException as exc:
        return None, exc
    return result, None


async def run_concurrent_turns_on_one_session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    manager: MagicMock,
    browser_states: tuple[MagicMock, MagicMock],
    exit_order: tuple[int, int] = (0, 1),
    after_first_exit: Callable[[], None] = lambda: None,
) -> None:
    """Two turns attached to one session, each on its own generation, released in ``exit_order``;
    ``after_first_exit`` is the point where only the earlier turn's release has run."""
    attached = (asyncio.Event(), asyncio.Event())
    may_exit = (asyncio.Event(), asyncio.Event())
    arrivals = count()

    async def _gate() -> None:
        index = next(arrivals)
        attached[index].set()
        await may_exit[index].wait()

    def _turn(index: int) -> asyncio.Task[tuple[AgentResult | None, BaseException | None]]:
        return asyncio.ensure_future(
            run_turn_to_exit(
                monkeypatch,
                manager=manager,
                exit_path="normal",
                browser_state=browser_states[index],
                on_attached=lambda _ctx: _gate(),
            )
        )

    first = _turn(0)
    await asyncio.wait_for(attached[0].wait(), 5)
    second = _turn(1)
    await asyncio.wait_for(attached[1].wait(), 5)
    turns = (first, second)

    may_exit[exit_order[0]].set()
    await turns[exit_order[0]]
    after_first_exit()
    may_exit[exit_order[1]].set()
    await turns[exit_order[1]]


async def run_turn_attaching_during_release(
    monkeypatch: pytest.MonkeyPatch,
    *,
    manager: MagicMock,
    browser_states: tuple[MagicMock, MagicMock],
    lookup_already_in_flight: bool = False,
) -> None:
    """A second turn resolves the session while the first turn's exit evict is still in flight.
    The manager hands out the first generation until that evict completes and the second after.
    With ``lookup_already_in_flight`` the second turn's lookup was issued before the release began
    and only answers once the evict is under way."""
    first_attached, evicting, evicted, contested = (asyncio.Event() for _ in range(4))

    async def _evict(*_args: object, **_kwargs: object) -> bool:
        if evicted.is_set():
            return True
        evicting.set()
        with suppress(TimeoutError):
            await asyncio.wait_for(contested.wait(), 1.0)
        evicted.set()
        return True

    async def _current_generation(**_kwargs: object) -> MagicMock:
        first_attached.set()
        if evicting.is_set() and not evicted.is_set():
            contested.set()
        return browser_states[1] if evicted.is_set() else browser_states[0]

    async def _generation_answered_mid_release(**kwargs: object) -> MagicMock:
        await asyncio.wait_for(evicting.wait(), 5)
        return await _current_generation(**kwargs)

    manager.evict_cached_browser_state = AsyncMock(side_effect=_evict)
    first = asyncio.ensure_future(
        run_turn_to_exit(
            monkeypatch,
            manager=manager,
            exit_path="normal",
            browser_state=browser_states[0],
            get_browser_state=AsyncMock(side_effect=_current_generation),
        )
    )
    second_lookup = _current_generation
    if lookup_already_in_flight:
        await asyncio.wait_for(first_attached.wait(), 5)
        second_lookup = _generation_answered_mid_release
    else:
        await asyncio.wait_for(evicting.wait(), 5)
    await run_turn_to_exit(
        monkeypatch,
        manager=manager,
        exit_path="normal",
        browser_state=browser_states[1],
        get_browser_state=AsyncMock(side_effect=second_lookup),
    )
    await first


async def run_turn_cancelled_during_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    manager: MagicMock,
    browser_state: MagicMock,
    evicting: asyncio.Event,
    on_attached: Callable[[CopilotContext], Awaitable[None]] | None = None,
) -> tuple[AgentResult | None, BaseException | None]:
    """Cancel the turn once its exit release has reached the manager, which is the only window in
    which a cancel can land on the finalizer rather than on the turn's own work."""
    turn = asyncio.ensure_future(
        run_turn_to_exit(
            monkeypatch,
            manager=manager,
            exit_path="normal",
            browser_state=browser_state,
            on_attached=on_attached,
        )
    )
    await asyncio.wait_for(evicting.wait(), 5)
    turn.cancel()
    return await turn


def install_org_secondary_llm_override(monkeypatch: pytest.MonkeyPatch, handler: object) -> None:
    """Make ``get_org_aware_secondary_llm_api_handler`` resolve an org's routed secondary model."""
    monkeypatch.setattr(
        api_handler_factory.skyvern_context,
        "current",
        lambda: SkyvernContext(organization_id="o_test", org_default_secondary_llm_key="CUSTOM_LLM_oat_fast"),
    )
    monkeypatch.setattr(api_handler_factory, "is_custom_llm_owned_by_organization", lambda _id, _org: True)
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "is_registered", lambda _key: True)
    monkeypatch.setattr(api_handler_factory.LLMAPIHandlerFactory, "get_llm_api_handler", lambda _key: handler)


class FakeCopilotStream:
    """EventSourceStream stand-in recording what was sent and whether the send was accepted."""

    def __init__(self, send_ok: bool = True) -> None:
        self.send_ok = send_ok
        self.sent: list[Any] = []

    async def send(self, payload: Any) -> bool:
        self.sent.append(payload)
        return self.send_ok

    async def is_disconnected(self) -> bool:
        return False

    def titles(self) -> list[str]:
        return [event.title for event in self.sent if isinstance(event, WorkflowCopilotTitleUpdate)]
