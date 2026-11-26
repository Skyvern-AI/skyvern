from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pydantic_core
import pytest
import pytest_asyncio
from agents import (
    Agent,
    FunctionTool,
    ModelResponse,
    ModelSettings,
    RunConfig,
    Runner,
    Tool,
    TResponseInputItem,
    Usage,
)
from agents.items import TResponseOutputItem
from agents.models.interface import Model
from agents.tool_context import ToolContext
from jsonschema import Draft202012Validator
from litellm.types.utils import ModelResponse as LitellmResponse
from openai.types.responses import ResponseFunctionToolCall
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import JsonValue, ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from skyvern.config import settings
from skyvern.core.script_generations import real_skyvern_page_ai
from skyvern.core.script_generations.real_skyvern_page_ai import RealSkyvernPageAi, _render_template_with_label
from skyvern.core.script_generations.script_skyvern_page import ScriptSkyvernPage
from skyvern.exceptions import ScrapingFailedBlankPage
from skyvern.forge import app
from skyvern.forge.sdk.agents.skills import (
    EVERYONE_SKILL_LABEL,
    MAX_PACKAGE_FILES,
    MAX_SKILL_RESULT_CHARS,
    AvailableSkill,
    SkillLoaded,
    SkillPackage,
    SkillResource,
    skill_result_chars,
)
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.forge.sdk.api.llm.model_usage import ModelUsageEvent, _observe_model_usage
from skyvern.forge.sdk.artifact.manager import ArtifactManager
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.artifact.storage.local import LocalStorage
from skyvern.forge.sdk.cache.local import LocalCache
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import GoogleOAuthCredentialModel, WorkflowRunBlockModel, WorkflowRunModel
from skyvern.forge.sdk.experimentation.providers import NoOpExperimentationProvider
from skyvern.forge.sdk.integrations.connected_accounts import (
    OrgConnection,
    _run_accounts_key,
    record_run_connection_ids,
)
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.google_oauth import GOOGLE_GMAIL_SEND_SCOPE
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.schemas.workflow_runs import AgentStepToolCall
from skyvern.forge.sdk.services.google_oauth_service import GOOGLE_GMAIL_READONLY_SCOPE, GOOGLE_SHEETS_DATA_SCOPE
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager, WorkflowRunContext
from skyvern.forge.sdk.workflow.exceptions import InvalidWorkflowDefinition
from skyvern.forge.sdk.workflow.models import agent_step_loop, agent_step_tools
from skyvern.forge.sdk.workflow.models.agent_step_block import AgentStepBlock
from skyvern.forge.sdk.workflow.models.block import (
    BaseTaskBlock,
    ConditionalBlock,
    DownloadToS3Block,
    ExtractionBlock,
    HttpRequestBlock,
    LoginBlock,
    TaskBlock,
)
from skyvern.forge.sdk.workflow.models.parameter import (
    PLATFORM_SMTP_AWS_KEYS,
    AWSSecretParameter,
    WorkflowParameter,
    WorkflowParameterType,
)
from skyvern.forge.sdk.workflow.workflow_definition_converter import block_yaml_to_block, convert_workflow_definition
from skyvern.forge.taskv3.opaque_refs import mask_opaque_urls
from skyvern.schemas.llm import LLMConfig
from skyvern.schemas.workflows import (
    AgentStepBlockYAML,
    AWSSecretParameterYAML,
    BlockResult,
    BlockStatus,
    BlockType,
    BranchConditionYAML,
    BranchCriteriaYAML,
    ConditionalBlockYAML,
    SendEmailBlockYAML,
    WorkflowDefinitionYAML,
)
from skyvern.services import script_service
from skyvern.utils.secret_redaction import REDACTED_SECRET_PLACEHOLDER
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import (
    TASK_V3_ACTION_DESCRIPTION_PREFIX,
    Action,
    ActionStatus,
    GotoUrlAction,
    UploadFileAction,
)
from tests.unit._script_page_fakes import ScrapedPageStub, build_navigation_page
from tests.unit.conftest import make_block_output_parameter
from tests.unit.ember_test_helpers import (
    RecordingSkills,
    available_skill,
    scripted_call,
    scripted_text,
    skill_md,
    tool_outputs_for,
)
from tests.unit.test_context_manager import _initialize_smtp_workflow, _make_workflow, _platform_email
from tests.unit.test_send_email_block import offline_address_validation  # noqa: F401

RUN = "wr_agent_step"
ORG = "o_agent_step"
WPID = "wpid_agent_step"
SECRET = "sk-live-SECRET-123456"
LUNA_MEDIUM = "OPENAI_GPT6_LUNA_MEDIUM"
HAIKU_MEDIUM = "ANTHROPIC_CLAUDE5_5_HAIKU_MEDIUM"
HAIKU = "anthropic/claude-haiku-5-5"
MODEL_CALL_COST = 0.25


def _pinned(effort: str, model_name: str = "openai/test") -> LLMConfig:
    return LLMConfig(
        model_name,
        [],
        supports_vision=False,
        add_assistant_prefix=False,
        reasoning_effort=effort,
        pin_reasoning_effort=True,
    )


# A callable turn writes its calls from the input the model received on that turn.
_Turn = list[TResponseOutputItem] | Callable[[list[Any]], list[TResponseOutputItem]]


class _ScriptedModel(Model):
    def __init__(self, script: list[_Turn]) -> None:
        self.script = script
        self.inputs: list[list[str | list[TResponseInputItem] | None]] = []
        self.tool_names: list[list[str]] = []
        self.tools: list[list[Tool]] = []
        self.tool_descriptions: list[dict[str, str]] = []

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        *args: Any,
        **kwargs: Any,
    ) -> ModelResponse:
        self.inputs.append([system_instructions, input])
        self.tool_names.append([tool.name for tool in tools])
        self.tools.append(tools)
        self.tool_descriptions.append({tool.name: tool.description for tool in tools if isinstance(tool, FunctionTool)})
        _observe_model_usage(ModelUsageEvent(request_model="scripted", cost=MODEL_CALL_COST))
        turn = self.script[len(self.inputs) - 1]
        output = turn(cast(list[Any], input)) if callable(turn) else turn
        return ModelResponse(output=output, usage=Usage(requests=1), response_id=None)

    def stream_response(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("the agent block does not stream")


class _FlagProvider(NoOpExperimentationProvider):
    def __init__(self, variant: str | None) -> None:
        super().__init__()
        self.variant = variant
        self.reads: list[tuple[str, dict | None]] = []

    async def _get_value(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> str | None:
        if feature_name != "AGENT_BLOCK_LLM_NAME":
            return None
        self.reads.append((distinct_id, properties))
        return self.variant


@pytest.fixture
def child_prompts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    prompts: list[str] = []

    async def fake_llm(prompt: str, prompt_name: str, **_: object) -> dict[str, str]:
        if prompt_name == "generate-workflow-run-block-description":
            return {"summary": "description"}
        prompts.append(prompt)
        _observe_model_usage(ModelUsageEvent(request_model="child", cost=100.0))
        return {"llm_response": f"the token is {SECRET}"}

    monkeypatch.setattr(app, "SECONDARY_LLM_API_HANDLER", fake_llm)
    return prompts


@pytest_asyncio.fixture
async def database(monkeypatch: pytest.MonkeyPatch, sqlite_engine: AsyncEngine, child_prompts: list[str]) -> AgentDB:
    database = AgentDB("sqlite+aiosqlite://", db_engine=sqlite_engine)
    monkeypatch.setattr(app, "DATABASE", database)
    monkeypatch.setattr(app, "WORKFLOW_CONTEXT_MANAGER", WorkflowContextManager())
    async with database.Session() as session:
        session.add(
            WorkflowRunModel(
                workflow_run_id=RUN,
                workflow_id="w_1",
                workflow_permanent_id=WPID,
                organization_id=ORG,
                status="running",
            )
        )
        await session.commit()
    run_context = WorkflowRunContext(
        workflow_title="t",
        workflow_id="w_1",
        workflow_permanent_id=WPID,
        workflow_run_id=RUN,
        aws_client=MagicMock(get_secret=AsyncMock(return_value=None)),
    )
    run_context.organization_id = ORG
    run_context.secrets["api_token"] = SECRET
    run_context.values["api_token"] = SECRET
    app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts[RUN] = run_context
    return database


@pytest.fixture
def billing_hook(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    hook = AsyncMock()
    monkeypatch.setattr(app.AGENT_FUNCTION, "post_agent_step_execution", hook)
    return hook


def _page_ai(scraped_url: str) -> RealSkyvernPageAi:
    ai = RealSkyvernPageAi.__new__(RealSkyvernPageAi)
    ai.scraped_page = SimpleNamespace(url=scraped_url)
    return ai


@pytest.fixture
def page(monkeypatch: pytest.MonkeyPatch) -> ScriptSkyvernPage:
    page, raw_page = build_navigation_page(ScriptSkyvernPage, ScrapedPageStub(), _page_ai("https://example.com/"))
    assert isinstance(page, ScriptSkyvernPage)
    raw_page.url = "https://example.com/"
    page.extract = AsyncMock(return_value="extracted")
    page.click = AsyncMock()
    page.fill = AsyncMock()

    async def create(browser_session_id: str | None = None) -> ScriptSkyvernPage:
        return page

    monkeypatch.setattr(ScriptSkyvernPage, "create", create)
    return page


def _billed_decisions(hook: AsyncMock) -> int:
    hook.assert_awaited_once()
    assert hook.await_args is not None
    assert hook.await_args.args[:2] == (ORG, RUN)
    return hook.await_args.kwargs["decisions"]


def _litellm_replies(
    monkeypatch: pytest.MonkeyPatch, *replies: dict[str, Any], usage: dict[str, int] | None = None
) -> list[dict[str, Any]]:
    """Answer each litellm.acompletion call with the next choice and return the requests sent."""
    sent: list[dict[str, Any]] = []

    async def fake_acompletion(**kwargs: object) -> LitellmResponse:
        sent.append(kwargs)
        return LitellmResponse(choices=[{"index": 0, **replies[len(sent) - 1]}], usage=usage)

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    return sent


def _litellm_call(name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
    function = {"name": name, "arguments": json.dumps(arguments)}
    return {
        "finish_reason": "tool_calls",
        "message": {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "function": function}]},
    }


@pytest.fixture(autouse=True)
def _no_cloud_integration_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    # The stub app's agent functions are async mocks; the catalog reads cloud entries synchronously.
    monkeypatch.setattr(app.AGENT_FUNCTION, "get_cloud_integration_entries", list)
    monkeypatch.setattr(app.AGENT_FUNCTION, "get_cloud_integration_triggers", dict)


async def _run_agent_block(
    monkeypatch: pytest.MonkeyPatch,
    variant: str | None,
    model: _ScriptedModel | None,
    max_turns: int = 25,
    prompt: str = "Find the answer.",
    tools: list[str] | None = sorted(agent_step_tools.tool_descriptions()),
    parameter_keys: list[str] | None = None,
    stored_tools: list[str] | None = None,
    block_model: dict[str, str | dict[str, str]] | None = None,
    data_schema: dict[str, Any] | None = None,
    **block_fields: bool,
) -> tuple[BlockResult, _FlagProvider]:
    """With no model, the block resolves the variant's real model, which sends through litellm.acompletion."""
    provider = _FlagProvider(variant)
    monkeypatch.setattr(app, "EXPERIMENTATION_PROVIDER", provider)
    if model is not None:
        monkeypatch.setattr(
            agent_step_loop, "resolve_agent_model", lambda _key: (model, RunConfig(tracing_disabled=True))
        )
    block = block_yaml_to_block(
        AgentStepBlockYAML(
            label="agent",
            prompt=prompt,
            max_turns=max_turns,
            tools=tools,
            parameter_keys=parameter_keys,
            model=block_model,
            **({"data_schema": data_schema} if data_schema is not None else {}),
            **block_fields,
        ),
        {
            "agent_output": make_block_output_parameter("agent_output"),
            **app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts[RUN].parameters,
        },
    )
    if stored_tools is not None:
        block = AgentStepBlock.model_validate({**block.model_dump(), "tools": stored_tools})
    return await block.execute_safe(workflow_run_id=RUN, organization_id=ORG), provider


VALID_FINISH = [scripted_call("finish", {"answer": "42"}, call_id="c3")]
PLAIN_TEXT = [scripted_text("The answer is 42.")]
ANOTHER_BLOCK = [
    scripted_call("run_block", {"block_type": "text_prompt", "arguments": {"prompt": "again"}}, call_id="c3")
]
ALL_TOOLS = [
    "browser_task",
    "web_search",
    "run_block",
    "read_run_data",
    "page_goto",
    "page_click",
    "page_fill",
    "page_extract",
    "finish",
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("max_turns", "script_tail", "stop_reason", "answer"),
    [
        (3, [VALID_FINISH], "completed", {"answer": "42"}),
        (2, [VALID_FINISH], "max_turns", {"answer": "42"}),
        (2, [PLAIN_TEXT], "max_turns", None),
        (2, [ANOTHER_BLOCK], "max_turns", None),
        (5, [PLAIN_TEXT, VALID_FINISH], "no_tool_calls", {"answer": "42"}),
        (5, [PLAIN_TEXT, PLAIN_TEXT], "no_tool_calls", None),
        (5, [PLAIN_TEXT, ANOTHER_BLOCK], "no_tool_calls", None),
    ],
)
async def test_agent_block_runs_tools_as_child_blocks_until_a_valid_finish(
    monkeypatch: pytest.MonkeyPatch,
    database: AgentDB,
    child_prompts: list[str],
    billing_hook: AsyncMock,
    max_turns: int,
    script_tail: list[list[TResponseOutputItem]],
    stop_reason: str,
    answer: dict[str, str] | None,
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    # When the turns run out or the model replies in plain text without a valid finish, one more turn follows on which
    # only finish runs.
    model = _ScriptedModel(
        [
            [
                scripted_call(
                    "run_block",
                    {"block_type": "text_prompt", "arguments": {"prompt": "Echo {{ api_token }}"}},
                    call_id="c1",
                )
            ],
            [scripted_call("finish", {}, call_id="c2")],
            *script_tail,
        ]
    )

    result, provider = await _run_agent_block(monkeypatch, LUNA_MEDIUM, model, max_turns=max_turns)

    rows = await database.observer.get_workflow_run_blocks(workflow_run_id=RUN, organization_id=ORG)
    (agent_row,) = (row for row in rows if row.block_type == BlockType.AGENT_STEP)
    children = [row for row in rows if row.parent_workflow_run_block_id == agent_row.workflow_run_block_id]
    assert [(row.block_type, row.status) for row in children] == [(BlockType.TEXT_PROMPT, "completed")]
    assert any("Echo {{ api_token }}" in prompt for prompt in child_prompts)
    assert SECRET not in json.dumps(model.inputs, default=str)
    assert "missing required property answer" in tool_outputs_for(model.inputs[2][1], "c2")[0]
    # Claude's replayed thinking is bound to the tool list it was produced under.
    assert all(names == ALL_TOOLS for names in model.tool_names)
    status = BlockStatus.completed if answer else BlockStatus.failed
    assert (result.status, agent_row.status) == (status, status.value)
    output = result.output_parameter_value
    assert isinstance(output, dict)
    assert output["stop_reason"] == stop_reason
    assert output["output"] == answer
    assert output["model"] == {"llm_key": LUNA_MEDIUM, "reasoning_effort": "medium"}
    assert [(call["tool"], call["status"]) for call in output["tool_calls"]][:2] == [
        ("run_block", "completed"),
        ("finish", "invalid_arguments"),
    ]
    assert provider.reads == [(RUN, {"organization_id": ORG, "workflow_permanent_id": WPID})]
    async with database.Session() as session:
        llm_cost = await session.scalar(
            select(WorkflowRunBlockModel.llm_cost).where(
                WorkflowRunBlockModel.workflow_run_block_id == agent_row.workflow_run_block_id
            )
        )
    assert len(model.inputs) == len(model.script)
    assert float(llm_cost) == pytest.approx(len(model.script) * MODEL_CALL_COST)
    assert _billed_decisions(billing_hook) == 1


@pytest.mark.asyncio
async def test_each_model_call_is_stored_on_the_step_row_as_the_model_received_it(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, tmp_path: Path
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    monkeypatch.setattr(app, "ARTIFACT_MANAGER", ArtifactManager())
    monkeypatch.setattr(app, "STORAGE", LocalStorage(str(tmp_path)))
    model = _ScriptedModel(
        [
            [
                scripted_call(
                    "run_block",
                    {"block_type": "text_prompt", "arguments": {"prompt": "Echo {{ api_token }}"}},
                    call_id="c1",
                )
            ],
            [scripted_call("read_run_data", {}, call_id="c2")],
            VALID_FINISH,
        ]
    )

    result, _ = await _run_agent_block(monkeypatch, LUNA_MEDIUM, model)

    assert result.status == BlockStatus.completed
    rows = await database.observer.get_workflow_run_blocks(workflow_run_id=RUN, organization_id=ORG)
    (agent_row,) = (row for row in rows if row.block_type == BlockType.AGENT_STEP)
    artifacts = await database.artifacts.get_artifacts_by_entity_id(
        organization_id=ORG,
        workflow_run_block_id=agent_row.workflow_run_block_id,
        artifact_types=[ArtifactType.LLM_REQUEST, ArtifactType.LLM_RESPONSE],
    )
    assert all((artifact.workflow_run_id, artifact.task_id) == (RUN, None) for artifact in artifacts)
    bodies: dict[ArtifactType, list[Any]] = {ArtifactType.LLM_REQUEST: [], ArtifactType.LLM_RESPONSE: []}
    for artifact in artifacts:
        stored = await app.ARTIFACT_MANAGER.retrieve_artifact(artifact)
        assert stored is not None
        assert SECRET not in stored.decode()
        bodies[artifact.artifact_type].append(json.loads(stored))
    assert [len(bodies[artifact_type]) for artifact_type in bodies] == [3, 3]
    requests = sorted(bodies[ArtifactType.LLM_REQUEST], key=lambda body: len(json.dumps(body["input"])))
    assert [[body["system_prompt"], body["input"]] for body in requests] == pydantic_core.to_jsonable_python(
        model.inputs
    )
    assert all([tool["name"] for tool in body["tools"]] == ALL_TOOLS for body in requests)
    assert all(tool["description"] and tool["parameters"]["type"] == "object" for tool in requests[0]["tools"])
    assert "*****" in tool_outputs_for(requests[1]["input"], "c1")[0]
    replies = [item["call_id"] for body in bodies[ArtifactType.LLM_RESPONSE] for item in body["output"]]
    assert sorted(replies) == ["c1", "c2", "c3"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["create_workflow_run_block_artifacts", "wait_for_upload_aiotasks"])
async def test_a_failed_model_call_store_leaves_the_step_outcome_and_model_input_unchanged(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, failing: str
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    monkeypatch.setattr(app.ARTIFACT_MANAGER, failing, AsyncMock(side_effect=OSError("storage down")))
    model = _ScriptedModel([VALID_FINISH])

    result, _ = await _run_agent_block(monkeypatch, LUNA_MEDIUM, model)

    assert result.status == BlockStatus.completed
    assert result.output_parameter_value["output"] == {"answer": "42"}
    assert [prompt for _system, prompt in model.inputs] == [[{"content": "Find the answer.", "role": "user"}]]


@pytest.mark.asyncio
async def test_authors_are_shown_exactly_the_tools_the_step_gets(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    model = _ScriptedModel([VALID_FINISH])

    await _run_agent_block(monkeypatch, LUNA_MEDIUM, model)

    shown = agent_step_tools.tool_descriptions()
    (given,) = model.tool_descriptions
    run_block = given.pop("run_block")
    assert given == {name: shown[name] for name in given}
    # This run has no credential parameter, no skills and no account for a catalog action, so the step is not given
    # login, load_skill or any catalog action.
    catalog = set(agent_step_tools.CATALOG_ACTION_TOOLS)
    assert set(given) | set(agent_step_tools.RUN_BLOCK_TYPES) | {"login", "load_skill"} | catalog == set(shown)
    options = {option.name for option in agent_step_tools.tool_options()}
    assert "load_skill" in options
    assert "read_skill_resource" not in options
    assert all(shown[name].rstrip(".") in run_block for name in agent_step_tools.RUN_BLOCK_TYPES)


def _credential_parameter() -> WorkflowParameter:
    now = datetime.now(UTC)
    return WorkflowParameter(
        key="login_credential",
        workflow_parameter_id="wp_credential",
        workflow_parameter_type=WorkflowParameterType.CREDENTIAL_ID,
        workflow_id="w_1",
        created_at=now,
        modified_at=now,
    )


def _file_parameter(key: str) -> WorkflowParameter:
    now = datetime.now(UTC)
    return WorkflowParameter(
        key=key,
        workflow_parameter_id=f"wp_{key}",
        workflow_parameter_type=WorkflowParameterType.FILE_URL,
        workflow_id="w_1",
        created_at=now,
        modified_at=now,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tools", "credential", "listed", "offered", "block_types"),
    [
        (
            ["http_request", "web_search"],
            False,
            False,
            ["web_search", "run_block", "read_run_data", "finish"],
            ["http_request"],
        ),
        (None, False, False, ["read_run_data", "finish"], None),
        ([], False, False, ["read_run_data", "finish"], None),
        (["web_search"], False, False, ["web_search", "read_run_data", "finish"], None),
        (["login", "page_goto"], False, False, ["read_run_data", "page_goto", "finish"], None),
        (["login", "page_goto"], True, False, ["read_run_data", "page_goto", "finish"], None),
        (["login", "page_goto"], True, True, ["login", "read_run_data", "page_goto", "finish"], None),
    ],
)
async def test_a_step_is_offered_only_the_tools_it_lists_with_finish_and_read_run_data(
    monkeypatch: pytest.MonkeyPatch,
    database: AgentDB,
    tools: list[str] | None,
    credential: bool,
    listed: bool,
    offered: list[str],
    block_types: list[str] | None,
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    if credential:
        app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts[RUN].parameters["login_credential"] = _credential_parameter()
    model = _ScriptedModel([VALID_FINISH])

    await _run_agent_block(
        monkeypatch, LUNA_MEDIUM, model, tools=tools, parameter_keys=["login_credential"] if listed else None
    )

    (given,) = model.tools
    assert [tool.name for tool in given] == offered
    run_block = next((tool for tool in given if isinstance(tool, FunctionTool) and tool.name == "run_block"), None)
    if block_types is None:
        assert run_block is None
        return
    assert run_block is not None
    assert run_block.params_json_schema["properties"]["block_type"]["enum"] == block_types
    described = {name for name in agent_step_tools.RUN_BLOCK_TYPES if f"- {name}(" in run_block.description}
    assert described == set(block_types)


@pytest.mark.asyncio
async def test_a_stored_step_listing_pdf_parser_is_offered_file_url_parser(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    model = _ScriptedModel([VALID_FINISH])

    await _run_agent_block(monkeypatch, LUNA_MEDIUM, model, tools=[], stored_tools=["pdf_parser"])

    (given,) = model.tools
    assert [tool.name for tool in given] == ["run_block", "read_run_data", "finish"]
    run_block = next(tool for tool in given if isinstance(tool, FunctionTool) and tool.name == "run_block")
    assert run_block.params_json_schema["properties"]["block_type"]["enum"] == ["file_url_parser"]
    offered = [
        [tool.name, tool.description, tool.params_json_schema] for tool in given if isinstance(tool, FunctionTool)
    ]
    assert len(offered) == len(given)
    assert "pdf_parser" not in json.dumps(offered)


SHEET_URL = "https://docs.google.com/spreadsheets/d/sheet_id/edit"


@pytest.fixture
def sheets_writes(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Records each Sheets values update the run sends."""
    monkeypatch.setattr(app.AGENT_FUNCTION, "get_google_sheets_credentials", AsyncMock(return_value="token"))
    sent: list[dict[str, Any]] = []

    async def write(**kwargs: Any) -> dict[str, Any]:
        sent.append(kwargs)
        return {"updatedRange": "Sheet1!A1", "updatedRows": 1}

    monkeypatch.setattr(app.AGENT_FUNCTION, "google_sheets_values_update", write)
    return sent


def _offered_run_block(model: _ScriptedModel) -> FunctionTool:
    return next(tool for tool in model.tools[0] if isinstance(tool, FunctionTool) and tool.name == "run_block")


@pytest.mark.asyncio
@pytest.mark.parametrize("tools", [["text_prompt"], ["text_prompt", "wait"]])
async def test_run_block_offers_each_listed_blocks_argument_schema_before_the_first_call(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, tools: list[str]
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    calls = {
        "text_prompt": ({"prompt": "Hi"}, {"prompt": ["Hi"]}),
        "wait": ({"wait_sec": 1}, {"wait_sec": "soon"}),
    }
    invalid = [
        [scripted_call("run_block", {"block_type": name, "arguments": calls[name][1]}, call_id=name)] for name in tools
    ]
    model = _ScriptedModel([*invalid, VALID_FINISH])

    await _run_agent_block(monkeypatch, LUNA_MEDIUM, model, tools=tools)

    offer = _offered_run_block(model).params_json_schema
    listed = offer["properties"]["block_type"]["enum"]
    arguments = offer["properties"]["arguments"]
    branches = [arguments] if len(listed) == 1 else arguments["anyOf"]
    for name, branch in zip(listed, branches, strict=True):
        report = json.loads(tool_outputs_for(model.inputs[-1][1], name)[0])
        assert report["status"] == "invalid_arguments", name
        recovery = report["argument_schema"]
        recovery_defs = recovery.pop("$defs", {})
        assert branch == recovery, name
        assert recovery_defs.items() <= offer.get("$defs", {}).items(), name
        recovery["$defs"] = recovery_defs
        good, bad = calls[name]
        for schema in ({**branch, "$defs": offer.get("$defs", {})}, recovery):
            assert Draft202012Validator(schema).is_valid(good), name
            assert not Draft202012Validator(schema).is_valid(bad), name
    assert set(re.findall(r'"#/\$defs/(\w+)"', json.dumps(offer))) <= set(offer.get("$defs", {}))


def _offered_accounts(model: _ScriptedModel, tool_name: str) -> list[str]:
    (tool,) = (tool for tool in model.tools[0] if isinstance(tool, FunctionTool) and tool.name == tool_name)
    return tool.params_json_schema["properties"]["connection_id"]["enum"]


@pytest.mark.asyncio
async def test_catalog_actions_are_offered_by_account_permission_and_run_through_the_integration_action_block(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, sheets_writes: list[dict[str, Any]]
) -> None:
    async with database.Session() as session:
        for account_id, scopes in [("goc_mail", [GOOGLE_GMAIL_READONLY_SCOPE]), ("goc_sheets", [SHEETS_SCOPE])]:
            session.add(
                GoogleOAuthCredentialModel(
                    id=account_id,
                    organization_id=ORG,
                    credential_name=account_id,
                    email_address=f"{account_id}@example.com",
                    state="active",
                    scopes_granted=scopes,
                )
            )
        await session.commit()
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    arguments = {
        "spreadsheet_url": SHEET_URL,
        "sheet_name": "Sheet1",
        "range": "A1",
        "write_mode": "update",
        "values": json.dumps([["{{ secret }}"]]),
        "connection_id": "goc_sheets",
    }
    model = _ScriptedModel(
        [
            [scripted_call("google_sheets__write_rows", {**arguments, "write_mode": "sideways"}, call_id="bad")],
            [scripted_call("google_sheets__write_rows", arguments, call_id="write")],
            VALID_FINISH,
        ]
    )
    listed = ["gmail__read_inbox", "gmail__send_email", "google_sheets__read_rows", "google_sheets__write_rows"]

    await _run_agent_block(monkeypatch, LUNA_MEDIUM, model, tools=[*listed, "powerbi__query"])

    offered = {tool.name for tool in model.tools[0] if isinstance(tool, FunctionTool) and "__" in tool.name}
    assert offered == {"gmail__read_inbox", "google_sheets__read_rows", "google_sheets__write_rows"}
    assert _offered_accounts(model, "gmail__read_inbox") == ["goc_mail"]
    assert _offered_accounts(model, "google_sheets__write_rows") == ["goc_sheets"]
    # The model is told exactly which listed actions it lacks, and none it was given.
    instructions = model.inputs[0][0]
    assert all(name in instructions for name in ("gmail__send_email", "powerbi__query"))
    assert not any(name in instructions for name in offered)
    bad = json.loads(tool_outputs_for(model.inputs[1][1], "bad")[0])
    assert bad["status"] == "invalid_arguments" and "write_mode" in bad["error"] and bad["argument_schema"]
    write = json.loads(tool_outputs_for(model.inputs[-1][1], "write")[0])
    assert write["status"] == "completed" and write["output"]["updated_range"] == "Sheet1!A1"
    # The model's text reaches the sheet as written: an agent's values are never rendered as templates.
    assert [sent["values"] for sent in sheets_writes] == [[["{{ secret }}"]]]
    rows = await database.observer.get_workflow_run_blocks(workflow_run_id=RUN, organization_id=ORG)
    assert [row.block_type for row in rows if row.block_type != BlockType.AGENT_STEP] == [BlockType.INTEGRATION_ACTION]


@pytest.mark.parametrize(
    ("saved", "current", "uses_account", "acts"),
    [
        ("pdf_parser", "file_url_parser", False, False),
        ("google_sheets_write", "google_sheets__write_rows", True, True),
        ("google_sheets_read", "google_sheets__read_rows", True, False),
        ("gmail__send_email", "gmail__send_email", True, True),
    ],
)
def test_a_saved_tool_name_reads_as_its_current_name_with_the_right_account_gating(
    saved: str, current: str, uses_account: bool, acts: bool
) -> None:
    block = AgentStepBlockYAML(label="agent", prompt="Find the answer.", tools=[saved, current])

    assert block.tools == [current]
    # Copilot asks for approved accounts by these sets, so a renamed tool must keep its place in them.
    assert (current in agent_step_tools.ACCOUNT_TOOL_NAMES, current in agent_step_tools.ACTING_ACCOUNT_TOOL_NAMES) == (
        uses_account,
        acts,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "listed", "told"),
    [
        ({"block_type": "text_prompt", "arguments": {"prompt": "Hi"}}, [], "['http_request']"),
        # A prompt written before Sheets became catalog tools still names the retired type: the reply sends the
        # model to the new tool only when this run offers it.
        (
            {"block_type": "google_sheets_write", "arguments": {"spreadsheet_url": SHEET_URL}},
            [],
            "google_sheets__write_rows, which this run does not offer",
        ),
        (
            {"block_type": "google_sheets_write", "arguments": {"spreadsheet_url": SHEET_URL}},
            ["google_sheets__write_rows"],
            "google_sheets__write_rows; call it directly",
        ),
    ],
)
async def test_a_run_block_call_for_an_unlisted_block_type_runs_no_child(
    monkeypatch: pytest.MonkeyPatch,
    database: AgentDB,
    child_prompts: list[str],
    call: dict[str, Any],
    listed: list[str],
    told: str,
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    await _google_accounts(database)
    model = _ScriptedModel(
        [
            [scripted_call("run_block", call, call_id="t")],
            [scripted_call("finish", {"answer": "done"}, call_id="f")],
        ]
    )

    await _run_agent_block(monkeypatch, LUNA_MEDIUM, model, tools=["http_request", *listed])

    rows = await database.observer.get_workflow_run_blocks(workflow_run_id=RUN, organization_id=ORG)
    assert [row.block_type for row in rows] == [BlockType.AGENT_STEP]
    assert child_prompts == []
    assert told in tool_outputs_for(model.inputs[1][1], "t")[0]


@pytest.mark.asyncio
async def test_a_call_to_an_unoffered_tool_name_returns_the_right_call_and_the_step_continues(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, tmp_path: Path
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    downloaded = tmp_path / "report.pdf"
    downloaded.write_bytes(b"%PDF")
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.block.resolve_local_or_download_file",
        AsyncMock(return_value=str(downloaded)),
    )
    monkeypatch.setattr(DownloadToS3Block, "_upload_file_to_s3", AsyncMock())
    url = "https://example.com/report.pdf"
    model = _ScriptedModel(
        [
            [
                scripted_call("run_block", {}, call_id="n").model_copy(update={"namespace": "other"}),
                scripted_call("download_to_s3", {"url": url}, call_id="u"),
                scripted_call("run_block", {"block_type": "download_to_s3", "arguments": {"url": url}}, call_id="r"),
            ],
            [scripted_call("open_file", {}, call_id="o")],
            VALID_FINISH,
        ]
    )

    with capture_logs() as logs:
        async with asyncio.timeout(10):
            result, _ = await _run_agent_block(monkeypatch, LUNA_MEDIUM, model)

    assert (result.status, result.failure_reason) == (BlockStatus.completed, None)
    assert isinstance(result.output_parameter_value, dict)
    assert result.output_parameter_value["stop_reason"] == "completed"
    (hint,) = tool_outputs_for(model.inputs[1][1], "u")
    assert "run_block" in hint and 'block_type="download_to_s3"' in hint
    (unknown,) = tool_outputs_for(model.inputs[2][1], "o")
    assert all(repr(name) in unknown for name in ALL_TOOLS)
    rows = await database.observer.get_workflow_run_blocks(workflow_run_id=RUN, organization_id=ORG)
    (agent_row,) = (row for row in rows if row.block_type == BlockType.AGENT_STEP)
    children = [row for row in rows if row.parent_workflow_run_block_id == agent_row.workflow_run_block_id]
    assert [(row.block_type, row.status) for row in children] == [(BlockType.DOWNLOAD_TO_S3, "completed")]
    assert all(names == ALL_TOOLS for names in model.tool_names)
    assert [
        (log["label"], log["tool_name"], log["workflow_run_id"], log["workflow_run_block_id"])
        for log in logs
        if log["event"] == "Agent step model called a tool it was not given"
    ] == [
        ("agent", "other.run_block", RUN, agent_row.workflow_run_block_id),
        ("agent", "download_to_s3", RUN, agent_row.workflow_run_block_id),
        ("agent", "open_file", RUN, agent_row.workflow_run_block_id),
    ]


@pytest.mark.parametrize("name", ["run_block", "update_workflow", "create_schedule", "not_a_tool", "gmail__send_emial"])
def test_a_tool_the_step_cannot_be_given_fails_at_save_and_the_error_lists_the_valid_tools(name: str) -> None:
    with pytest.raises(ValidationError) as error:
        AgentStepBlockYAML(label="agent", prompt="Find the answer.", tools=["http_request", name])

    message = str(error.value)
    assert f"Unknown tools [{name!r}]" in message
    # Every name the step can be given, catalog actions included, and nothing retired.
    listable = set(agent_step_tools.tool_descriptions()) | set(agent_step_tools.SAVED_BLOCK_ONLY_TOOLS)
    assert ", ".join(sorted(listable)) in message
    assert "pdf_parser" not in message


def test_a_tools_list_validates_without_openai_agents() -> None:
    script = (
        "import sys; sys.modules['agents'] = None; sys.modules['skyvern.cli.mcp_tools'] = None; "
        "from skyvern.schemas.workflows import AgentStepBlockYAML; "
        "print(AgentStepBlockYAML(label='a', prompt='p', tools=['http_request']).tools)"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("['http_request']")


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "I can"])
async def test_a_model_refusal_fails_the_agent_block_without_asking_again(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, tmp_path: Path, text: str
) -> None:
    # LiteLLM reports Anthropic's refusal stop reason as content_filter, with or without the text written before it.
    monkeypatch.setitem(LLMConfigRegistry._configs, HAIKU_MEDIUM, _pinned("medium", HAIKU))
    monkeypatch.setattr(app, "ARTIFACT_MANAGER", ArtifactManager())
    monkeypatch.setattr(app, "STORAGE", LocalStorage(str(tmp_path)))
    sent = _litellm_replies(
        monkeypatch, {"finish_reason": "content_filter", "message": {"role": "assistant", "content": text}}
    )

    result, _ = await _run_agent_block(monkeypatch, HAIKU_MEDIUM, None)

    assert len(sent) == 1
    assert result.status == BlockStatus.failed
    assert (result.failure_reason or "").startswith("The model refused: ")
    output = result.output_parameter_value
    assert isinstance(output, dict)
    assert (output["stop_reason"], output["output"]) == ("refused", None)
    (agent_row,) = await database.observer.get_workflow_run_blocks(workflow_run_id=RUN, organization_id=ORG)
    (reply,) = await database.artifacts.get_artifacts_by_entity_id(
        organization_id=ORG,
        workflow_run_block_id=agent_row.workflow_run_block_id,
        artifact_types=[ArtifactType.LLM_RESPONSE],
    )
    assert reply.task_id is None


@pytest.mark.asyncio
async def test_each_claude_call_caches_through_its_newest_tool_result(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, HAIKU_MEDIUM, _pinned("medium", HAIKU))
    sent = _litellm_replies(
        monkeypatch,
        _litellm_call("finish", {}, "c1"),
        _litellm_call("finish", {}, "c2"),
        _litellm_call("finish", {"answer": "42"}, "c3"),
    )

    result, _ = await _run_agent_block(monkeypatch, HAIKU_MEDIUM, None)

    assert result.status == BlockStatus.completed
    assert [request["cache_control_injection_points"] for request in sent[1:]] == [
        [{"location": "message", "index": len(request["messages"]) - 1}] for request in sent[1:]
    ]
    # Today's date follows the cached system prefix, so runs on other days still share that prefix.
    today = datetime.now(UTC).date().isoformat()
    stable, dated = sent[0]["messages"][0]["content"]
    assert "cache_control" in stable and today not in stable["text"] and today in dated["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["canceled", "timed_out"])
async def test_a_run_that_ends_during_a_model_turn_stops_the_agent_block(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, billing_hook: AsyncMock, ending: str
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))

    class RunEndingModel(_ScriptedModel):
        async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
            response = await super().get_response(*args, **kwargs)
            if len(self.inputs) == 1:
                return response
            if ending == "canceled":
                async with database.Session() as session:
                    await session.execute(update(WorkflowRunModel).values(status="canceled"))
                    await session.commit()
            else:
                # The run's elapsed timeout cancels the block's task.
                task = asyncio.current_task()
                assert task is not None
                task.cancel()
                await asyncio.sleep(0)
            return response

    model = RunEndingModel(
        [
            [scripted_call("read_run_data", {}, call_id="index")],
            [scripted_call("finish", {"answer": "42"}, call_id="finish")],
        ]
    )

    if ending == "canceled":
        result, _ = await _run_agent_block(monkeypatch, LUNA_MEDIUM, model)
        assert result.status == BlockStatus.canceled
    else:
        with pytest.raises(asyncio.CancelledError):
            await _run_agent_block(monkeypatch, LUNA_MEDIUM, model)
        task = asyncio.current_task()
        assert task is not None
        task.uncancel()

    assert len(model.inputs) == 2
    output = app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts[RUN].values["agent_output"]
    assert (output["stop_reason"], [call["tool"] for call in output["tool_calls"]]) == ("run_ended", ["read_run_data"])
    (agent_row,) = await database.observer.get_workflow_run_blocks(workflow_run_id=RUN, organization_id=ORG)
    async with database.Session() as session:
        llm_cost = await session.scalar(
            select(WorkflowRunBlockModel.llm_cost).where(
                WorkflowRunBlockModel.workflow_run_block_id == agent_row.workflow_run_block_id
            )
        )
    assert float(llm_cost) == pytest.approx(2 * MODEL_CALL_COST)
    assert _billed_decisions(billing_hook) == 1
    if ending == "canceled":
        assert agent_row.status == "canceled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("variant", "config", "accepted"),
    [
        (None, None, False),
        ("NOT_A_REGISTERED_KEY", None, False),
        ("CUSTOM_LLM_org_model", _pinned("high"), False),
        (
            "KEY_WITHOUT_PINNED_EFFORT",
            LLMConfig("openai/test", [], supports_vision=False, add_assistant_prefix=False, reasoning_effort="high"),
            False,
        ),
        ("OPENAI_GPT6_LUNA_MEDIUM", _pinned("medium"), True),
        ("OPENAI_GPT6_LUNA_HIGH", _pinned("high"), True),
        ("ANTHROPIC_CLAUDE5_5_HAIKU_MEDIUM_WITH_BEDROCK_FALLBACK", _pinned("medium"), True),
        ("ANTHROPIC_CLAUDE5_5_HAIKU_HIGH_WITH_BEDROCK_FALLBACK", _pinned("high"), True),
    ],
)
async def test_agent_block_runs_only_on_a_flag_variant_that_pins_model_and_effort(
    monkeypatch: pytest.MonkeyPatch,
    database: AgentDB,
    variant: str | None,
    config: LLMConfig | None,
    accepted: bool,
) -> None:
    if variant and config:
        monkeypatch.setitem(LLMConfigRegistry._configs, variant, config)
    model = _ScriptedModel([[scripted_call("finish", {"answer": "ok"}, call_id="c1")]])

    result, _ = await _run_agent_block(monkeypatch, variant, model)

    (row,) = await database.observer.get_workflow_run_blocks(workflow_run_id=RUN, organization_id=ORG)
    if accepted:
        assert row.status == "completed"
        assert isinstance(result.output_parameter_value, dict)
        assert result.output_parameter_value["model"]["llm_key"] == variant
    else:
        assert row.status == "failed"
        assert "AGENT_BLOCK_LLM_NAME" in (row.failure_reason or "")
        assert model.inputs == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("variant", "model_name", "expected_key", "reasons"),
    [
        (HAIKU_MEDIUM, None, HAIKU_MEDIUM, None),
        (HAIKU_MEDIUM, "luna-medium", LUNA_MEDIUM, None),
        (HAIKU_MEDIUM, "luna-unpinned", None, ("'luna-unpinned'", "'LUNA_UNPINNED'")),
        (HAIKU_MEDIUM, "not-in-the-list", HAIKU_MEDIUM, None),
        (HAIKU_MEDIUM, {"name": "luna-medium"}, HAIKU_MEDIUM, None),
        (None, "luna-medium", None, ("AGENT_BLOCK_LLM_NAME",)),
    ],
)
async def test_the_block_model_picks_a_pinned_key_only_after_the_flag_enables_the_block(
    monkeypatch: pytest.MonkeyPatch,
    database: AgentDB,
    variant: str | None,
    model_name: str | dict[str, str] | None,
    expected_key: str | None,
    reasons: tuple[str, ...] | None,
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, HAIKU_MEDIUM, _pinned("medium", HAIKU))
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    monkeypatch.setitem(
        LLMConfigRegistry._configs,
        "LUNA_UNPINNED",
        LLMConfig("openai/test", [], supports_vision=False, add_assistant_prefix=False, reasoning_effort="medium"),
    )
    mapping = {
        "luna-medium": {"llm_key": LUNA_MEDIUM, "label": "Luna (medium)"},
        "luna-unpinned": {"llm_key": "LUNA_UNPINNED", "label": "Luna"},
    }
    monkeypatch.setattr(
        type(SettingsManager.get_settings()),
        "get_model_name_to_llm_key",
        lambda _self, organization_id=None: mapping,
    )
    model = _ScriptedModel([[scripted_call("finish", {"answer": "ok"}, call_id="c1")]])

    result, _ = await _run_agent_block(
        monkeypatch, variant, model, block_model=None if model_name is None else {"model_name": model_name}
    )

    (row,) = await database.observer.get_workflow_run_blocks(workflow_run_id=RUN, organization_id=ORG)
    if expected_key:
        assert row.status == "completed"
        assert isinstance(result.output_parameter_value, dict)
        assert result.output_parameter_value["model"]["llm_key"] == expected_key
    else:
        assert row.status == "failed"
        assert reasons and all(reason in (row.failure_reason or "") for reason in reasons)
        assert model.inputs == []


@pytest.mark.asyncio
async def test_the_agent_block_sends_its_pinned_effort_on_a_bare_gpt_5_name(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _litellm_replies(
        monkeypatch,
        {"message": {"role": "assistant", "content": "ok"}},
        usage={"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
    )
    monkeypatch.setitem(LLMConfigRegistry._configs, "GPT5_PINNED_TEST", _pinned("high", "gpt-5.4"))
    model, run_config = agent_step_loop.resolve_agent_model("GPT5_PINNED_TEST")

    with capture_logs() as logs:
        await Runner.run(Agent(name="agent-step", model=model), "hello", run_config=run_config)

    (request,) = sent
    assert request["reasoning_effort"] == "high"
    (usage,) = (log for log in logs if log.get("log_code") == "model_usage")
    assert usage["gen_ai.agent.name"] == "agent_step"


def test_every_block_tool_schema_defines_exactly_the_definitions_it_references() -> None:
    state = cast(
        agent_step_loop._RunState, SimpleNamespace(credential_keys=["login_credential"], connected_accounts=[])
    )
    schemas = [
        (tool.yaml_class.__name__, agent_step_loop._arguments_schema(tool, state))
        for tool in agent_step_tools.AGENT_BLOCK_TOOLS.values()
        if isinstance(tool, agent_step_tools.BlockTool)
    ]
    schemas.append(("run_block", agent_step_loop._run_block_schema(state, list(agent_step_tools.RUN_BLOCK_TYPES))))
    for name, schema in schemas:
        references = set(re.findall(r'"#/\$defs/(\w+)"', json.dumps(schema)))
        assert references == set(schema.get("$defs", {})), name
    # run_block hoists every listed block's $defs into one root, so a shared name must mean the same thing.
    shared: dict[str, Any] = {}
    for _, tool in agent_step_tools.RUN_BLOCK_TYPES.values():
        for key, definition in agent_step_loop._arguments_schema(tool, state).get("$defs", {}).items():
            assert shared.setdefault(key, definition) == definition, key


def test_every_block_type_is_an_agent_tool_or_says_why_not() -> None:
    assert set(agent_step_tools.AGENT_BLOCK_TOOLS) == set(BlockType)
    for block_type, tool in agent_step_tools.AGENT_BLOCK_TOOLS.items():
        if isinstance(tool, str):
            assert tool
            continue
        assert tool.yaml_class.model_fields["block_type"].default == block_type
        assert set(tool.fields) - {"credential", "files"} <= set(tool.yaml_class.model_fields)
    # The account fields each tool offers: one dropped here leaves that tool unable to pick an account.
    offered = {
        block_type.value: tool.arguments[len(tool.fields) :]
        for block_type, tool in agent_step_tools.AGENT_BLOCK_TOOLS.items()
        if isinstance(tool, agent_step_tools.BlockTool) and len(tool.arguments) > len(tool.fields)
    }
    assert offered == {
        "file_upload": ("google_credential_id",),
        "send_email": ("credential_id",),
        "email_inbox": ("credential_id",),
    }


SHEETS_SCOPE = GOOGLE_SHEETS_DATA_SCOPE


async def _google_accounts(database: AgentDB, scopes: list[str] | None = None) -> None:
    async with database.Session() as session:
        for account_id, organization_id, state in [
            ("goc_active", ORG, "active"),
            ("goc_errored", ORG, "error"),
            ("goc_foreign", "o_other", "active"),
        ]:
            session.add(
                GoogleOAuthCredentialModel(
                    id=account_id,
                    organization_id=organization_id,
                    credential_name=account_id,
                    email_address=f"{account_id}@example.com",
                    state=state,
                    scopes_granted=scopes or [SHEETS_SCOPE],
                )
            )
        await session.commit()


@pytest.mark.asyncio
async def test_a_ember_run_offers_and_accepts_only_the_account_the_chat_approved(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB
) -> None:
    async with database.Session() as session:
        for account_id in ("goc_a", "goc_b", "goc_c"):
            session.add(
                GoogleOAuthCredentialModel(
                    id=account_id,
                    organization_id=ORG,
                    credential_name=account_id,
                    email_address=f"{account_id}@example.com",
                    state="active",
                    scopes_granted=[SHEETS_SCOPE],
                )
            )
        await session.execute(
            update(WorkflowRunModel).where(WorkflowRunModel.workflow_run_id == RUN).values(created_by="copilot")
        )
        await session.commit()
    monkeypatch.setattr(app, "CACHE", LocalCache())
    monkeypatch.setattr(settings, "EMBER_NARROW_AGENT_STEP_ACCOUNTS", False)
    # With no record, narrowing off keeps every account (an older API required them all); on offers none.
    assert {account.id for account in await agent_step_loop._connected_accounts(ORG, RUN)} == {
        "goc_a",
        "goc_b",
        "goc_c",
    }
    monkeypatch.setattr(settings, "EMBER_NARROW_AGENT_STEP_ACCOUNTS", True)
    assert await agent_step_loop._connected_accounts(ORG, RUN) == []
    await record_run_connection_ids(ORG, RUN, ["goc_a"])
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    sheet = {"spreadsheet_url": "https://docs.google.com/spreadsheets/d/x"}
    model = _ScriptedModel(
        [
            [scripted_call("google_sheets__read_rows", {**sheet, "connection_id": account}, call_id=account)]
            for account in ("goc_b", "goc_a")
        ]
        + [[scripted_call("finish", {"answer": "done"}, call_id="f")]]
    )

    await _run_agent_block(monkeypatch, LUNA_MEDIUM, model, tools=["google_sheets__read_rows"])

    assert _offered_accounts(model, "google_sheets__read_rows") == ["goc_a"]
    async with database.Session() as session:
        session.add(
            WorkflowRunModel(
                workflow_run_id="wr_child",
                workflow_id="w_1",
                workflow_permanent_id=WPID,
                organization_id=ORG,
                status="running",
                parent_workflow_run_id=RUN,
            )
        )
        await session.commit()
    assert [account.id for account in await agent_step_loop._connected_accounts(ORG, "wr_child")] == ["goc_a"]
    (refused,) = tool_outputs_for(model.inputs[1][1], "goc_b")
    (accepted,) = tool_outputs_for(model.inputs[2][1], "goc_a")
    assert "invalid_arguments" in refused and "goc_b@" not in refused
    assert "invalid_arguments" not in accepted


@pytest.mark.asyncio
async def test_a_ember_runs_record_holds_whatever_the_workers_setting_and_a_broken_chain_offers_none(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB
) -> None:
    async with database.Session() as session:
        for account_id in ("goc_a", "goc_b"):
            session.add(
                GoogleOAuthCredentialModel(
                    id=account_id,
                    organization_id=ORG,
                    credential_name=account_id,
                    email_address=f"{account_id}@example.com",
                    state="active",
                    scopes_granted=[SHEETS_SCOPE],
                )
            )
        await session.execute(
            update(WorkflowRunModel).where(WorkflowRunModel.workflow_run_id == RUN).values(created_by="copilot")
        )
        # wr_hop_1 is RUN's child, and each wr_hop_n the child of wr_hop_{n-1}; wr_dangling's parent is gone.
        for hop in range(1, agent_step_loop._MAX_PARENT_RUN_HOPS + 2):
            session.add(
                WorkflowRunModel(
                    workflow_run_id=f"wr_hop_{hop}",
                    workflow_id="w_1",
                    workflow_permanent_id=WPID,
                    organization_id=ORG,
                    status="running",
                    parent_workflow_run_id=RUN if hop == 1 else f"wr_hop_{hop - 1}",
                )
            )
        session.add(
            WorkflowRunModel(
                workflow_run_id="wr_dangling",
                workflow_id="w_1",
                workflow_permanent_id=WPID,
                organization_id=ORG,
                status="running",
                parent_workflow_run_id="wr_deleted",
            )
        )
        await session.commit()
    cache = LocalCache()
    monkeypatch.setattr(app, "CACHE", cache)
    await record_run_connection_ids(ORG, RUN, ["goc_a"])

    async def offered(run_id: str) -> list[str]:
        return sorted(account.id for account in await agent_step_loop._connected_accounts(ORG, run_id))

    # Narrowing off on this worker: a record is still enforced, and an unreadable one offers none.
    monkeypatch.setattr(settings, "EMBER_NARROW_AGENT_STEP_ACCOUNTS", False)
    assert await offered(RUN) == ["goc_a"]
    assert await offered(f"wr_hop_{agent_step_loop._MAX_PARENT_RUN_HOPS}") == ["goc_a"]
    await cache.set(_run_accounts_key(ORG, RUN), "not json")
    assert await offered(RUN) == []
    monkeypatch.setattr(cache, "get", AsyncMock(side_effect=RuntimeError("cache down")))
    assert await offered(RUN) == []
    assert await offered("wr_dangling") == ["goc_a", "goc_b"]

    monkeypatch.setattr(settings, "EMBER_NARROW_AGENT_STEP_ACCOUNTS", True)
    assert await offered("wr_dangling") == []
    assert await offered(f"wr_hop_{agent_step_loop._MAX_PARENT_RUN_HOPS + 1}") == []


@pytest.mark.asyncio
async def test_a_run_creators_connected_gmail_account_is_never_offered_to_the_agent_step(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB
) -> None:
    async with database.Session() as session:
        session.add(
            GoogleOAuthCredentialModel(
                id="goc_a",
                organization_id=ORG,
                credential_name="goc_a",
                email_address="goc_a@example.com",
                state="active",
                scopes_granted=[SHEETS_SCOPE],
            )
        )
        await session.execute(
            update(WorkflowRunModel).where(WorkflowRunModel.workflow_run_id == RUN).values(created_by="user-1")
        )
        await session.commit()

    async def own_connected_gmail(organization_id: str, user_id: str) -> list[OrgConnection]:
        return (
            [OrgConnection("ngc_owned", "nango", "Connected Gmail", None, "active", ())] if user_id == "user-1" else []
        )

    monkeypatch.setattr(app.AGENT_FUNCTION, "own_connected_gmail_connections", own_connected_gmail)
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    model = _ScriptedModel([VALID_FINISH])

    await _run_agent_block(monkeypatch, LUNA_MEDIUM, model, tools=["gmail__read_inbox"])

    # A Nango hop token exists only for a saved block, so the agent step cannot use the account (ADR 0033), and the
    # model is pointed at the saved block rather than told the account lacks a permission.
    assert not any(isinstance(tool, FunctionTool) and tool.name == "gmail__read_inbox" for tool in model.tools[0])
    assert "saved Gmail: Read inbox block" in model.inputs[0][0]


def _account_call(tool: str, account: str) -> Any:
    if tool == "email_inbox":
        arguments = {"email_client": "gmail", "prompt": "the code", "credential_id": account}
        return scripted_call("run_block", {"block_type": tool, "arguments": arguments}, call_id=account)
    return scripted_call(tool, {"spreadsheet_url": SHEET_URL, "connection_id": account}, call_id=account)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "scopes"),
    # A catalog action checks the account in its own tool; a block run_block runs checks it in _yaml_arguments.
    [("google_sheets__read_rows", [SHEETS_SCOPE]), ("email_inbox", [GOOGLE_GMAIL_READONLY_SCOPE])],
)
async def test_an_account_outside_the_runs_active_accounts_is_refused_with_the_valid_choices(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, tool: str, scopes: list[str]
) -> None:
    await _google_accounts(database, scopes)
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    model = _ScriptedModel(
        [[_account_call(tool, account)] for account in ("goc_foreign", "goc_errored")]
        + [[scripted_call("finish", {"answer": "done"}, call_id="f")]]
    )

    await _run_agent_block(monkeypatch, LUNA_MEDIUM, model, tools=[tool])

    if tool == "email_inbox":
        (run_block,) = (t for t in model.tools[0] if isinstance(t, FunctionTool) and t.name == "run_block")
        offered = run_block.params_json_schema["$defs"][agent_step_loop._ACCOUNT_DEF]["enum"]
    else:
        offered = _offered_accounts(model, tool)
    assert offered == ["goc_active"]
    for turn, account in enumerate(("goc_foreign", "goc_errored"), start=1):
        (output,) = tool_outputs_for(model.inputs[turn][1], account)
        assert "invalid_arguments" in output
        assert "goc_active" in output and "goc_errored@" not in output and "goc_foreign@" not in output
    rows = await database.observer.get_workflow_run_blocks(workflow_run_id=RUN, organization_id=ORG)
    assert [row.block_type for row in rows] == [BlockType.AGENT_STEP]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("accounts", "senders", "readers", "refusal_names"),
    [
        (
            [
                ("goc_reader", [GOOGLE_GMAIL_READONLY_SCOPE]),
                ("goc_sender", [GOOGLE_GMAIL_READONLY_SCOPE, GOOGLE_GMAIL_SEND_SCOPE]),
                # No recorded grant: whether it can send is unknown, so it must stay offered everywhere.
                ("goc_unrecorded", []),
            ],
            ["goc_sender", "goc_unrecorded"],
            ["goc_reader", "goc_sender", "goc_unrecorded"],
            "goc_sender",
        ),
        # Only a read-only account: none can send, so the refusal names the permission to reconnect with.
        ([("goc_reader", [GOOGLE_GMAIL_READONLY_SCOPE])], [], ["goc_reader"], GOOGLE_GMAIL_SEND_SCOPE),
    ],
)
async def test_a_read_only_gmail_account_is_not_offered_for_send_but_stays_for_reading(
    monkeypatch: pytest.MonkeyPatch,
    database: AgentDB,
    accounts: list[tuple[str, list[str]]],
    senders: list[str],
    readers: list[str],
    refusal_names: str,
) -> None:
    async with database.Session() as session:
        for account_id, scopes in accounts:
            session.add(
                GoogleOAuthCredentialModel(
                    id=account_id,
                    organization_id=ORG,
                    credential_name=account_id,
                    email_address=f"{account_id}@example.com",
                    state="active",
                    scopes_granted=scopes,
                )
            )
        await session.commit()
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    email = {**EMAIL, "transport": "gmail", "recipients": ["ops@example.com"], "credential_id": "goc_reader"}
    model = _ScriptedModel(
        [[scripted_call("run_block", {"block_type": "send_email", "arguments": email}, call_id="send")], VALID_FINISH]
    )

    await _run_agent_block(monkeypatch, LUNA_MEDIUM, model, tools=["email_inbox", "send_email"])

    (run_block,) = (tool for tool in model.tools[0] if isinstance(tool, FunctionTool) and tool.name == "run_block")
    schema = run_block.params_json_schema

    def offered(field: str) -> list[str]:
        branch = next(b for b in schema["properties"]["arguments"]["anyOf"] if field in b["properties"])
        return sorted(schema["$defs"][branch["properties"]["credential_id"]["$ref"].rpartition("/")[2]].get("enum", []))

    assert (offered("recipients"), offered("folder")) == (senders, readers)
    (refused,) = tool_outputs_for(model.inputs[1][1], "send")
    assert "invalid_arguments" in refused and refusal_names in refused and "goc_reader@" not in refused


CHILD_OUTPUT = "agent_text_prompt_1_output"
UPLOAD = {"url": "https://example.com", "navigation_goal": "Upload the file"}
HTTP_UPLOAD = {
    "block_type": "http_request",
    "arguments": {"method": "POST", "url": "https://example.com", "files": {"document": "{{ fax_file }}"}},
}
REPORT_URL = "https://bucket.example.com/report.json?X-Amz-Signature=" + "0a1b2c3d4e5f6789" * 4
REPORT = FileInfo(url=REPORT_URL, filename="report.json", checksum="abc", file_size=12)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_child", "call", "handed", "allowed"),
    [
        (None, ("browser_task", {**UPLOAD, "files": ["fax_file"]}), (["fax_file"], None), None),
        (None, ("browser_task", {**UPLOAD, "files": ["nope"]}), None, ["fax_file", "report.json", "stale.json"]),
        (
            None,
            ("browser_task", {**UPLOAD, "files": ["login_credential"]}),
            None,
            ["fax_file", "report.json", "stale.json"],
        ),
        ("completed", ("browser_task", {**UPLOAD, "files": [CHILD_OUTPUT]}), ([CHILD_OUTPUT], None), None),
        (
            "completed",
            ("browser_task", {**UPLOAD, "files": ["agent_text_prompt_1"]}),
            None,
            ["fax_file", "report.json", "stale.json", CHILD_OUTPUT],
        ),
        (
            "failed",
            ("browser_task", {**UPLOAD, "files": [CHILD_OUTPUT]}),
            None,
            ["fax_file", "report.json", "stale.json"],
        ),
        (None, ("run_block", HTTP_UPLOAD), ([], HTTP_UPLOAD["arguments"]["files"]), None),
        (None, ("browser_task", {**UPLOAD, "files": ["report.json"]}), (["report.json"], None), None),
        # A download named like a value the run already holds can't become a parameter of that name.
        (None, ("browser_task", {**UPLOAD, "files": ["stale.json"]}), None, "already has a value named 'stale.json'"),
    ],
)
async def test_browser_task_hands_over_only_this_runs_files_by_name(
    monkeypatch: pytest.MonkeyPatch,
    database: AgentDB,
    first_child: str | None,
    call: tuple[str, dict[str, Any]],
    handed: tuple[list[str], dict[str, str] | None] | None,
    allowed: list[str] | str | None,
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    context = app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts[RUN]
    context.parameters |= {"fax_file": _file_parameter("fax_file"), "login_credential": _credential_parameter()}
    # A value left by an earlier execution of this step, whose child labels repeat.
    context.values |= {"fax_file": "file_123", CHILD_OUTPUT: {"llm_response": "stale"}, "stale.json": "kept"}
    declared = dict(context.parameters)
    stale = FileInfo(url="https://example.com/stale.json", filename="stale.json")
    # report.json is registered after the step's first listing, so only a check made at call time accepts it.
    listings = [[], [REPORT, stale]]

    async def downloaded_files(organization_id: str, run_id: str | None) -> list[FileInfo]:
        return listings.pop(0) if len(listings) > 1 else listings[0]

    monkeypatch.setattr(app, "STORAGE", SimpleNamespace(get_current_attempt_downloaded_files=downloaded_files))
    if first_child == "failed":

        async def fail(*_: object, **__: object) -> NoReturn:
            raise RuntimeError("model unavailable")

        monkeypatch.setattr(app, "SECONDARY_LLM_API_HANDLER", fail)
    handed_over: list[tuple[list[str], dict[str, str] | None]] = []
    handed_values: dict[str, Any] = {}

    async def capture(block: TaskBlock | HttpRequestBlock, *_: object, **__: object) -> BlockResult:
        files = block.files if isinstance(block, HttpRequestBlock) else None
        handed_over.append(([parameter.key for parameter in block.parameters], files))
        handed_values.update({parameter.key: context.values[parameter.key] for parameter in block.parameters})
        return BlockResult(
            success=True,
            output_parameter=block.output_parameter,
            output_parameter_value={},
            status=BlockStatus.completed,
        )

    monkeypatch.setattr(TaskBlock, "execute", capture)
    monkeypatch.setattr(HttpRequestBlock, "execute", capture)
    script: list[_Turn] = [
        [scripted_call("read_run_data", {}, call_id="index")],
        [scripted_call(*call, call_id="task")],
        [scripted_call("read_run_data", {}, call_id="index_after")],
        VALID_FINISH,
    ]
    if first_child:
        run = {"block_type": "text_prompt", "arguments": {"prompt": "Split the fax"}}
        script.insert(0, [scripted_call("run_block", run, call_id="split")])
    model = _ScriptedModel(script)

    await _run_agent_block(
        monkeypatch, LUNA_MEDIUM, model, tools=["browser_task", "text_prompt", "http_request", "read_run_data"]
    )

    assert _tool_output(model, "index")["files"] == ["fax_file"]
    assert {"fax_file", "report.json"} <= set(_tool_output(model, "index_after")["files"])
    report = json.loads(tool_outputs_for(model.inputs[-1][1], "task")[0])
    if allowed is None:
        assert report["status"] == "completed"
        assert handed_over == [handed]
        assert handed_values.get("report.json", REPORT_URL) == REPORT_URL
    else:
        assert report["status"] == "invalid_arguments"
        assert str(allowed) in report["error"]
        assert handed_over == []
    assert context.parameters == declared
    assert "report.json" not in context.values


FORM_URL = "https://example.com/upload"
FAX_URL = "https://bucket.example.com/fax.pdf?X-Amz-Signature=" + "0123456789abcdef" * 4
NOTES_URL = "https://example.com/notes.pdf"
DECOY_URL = "https://example.com/resume.pdf?signature=" + "fedcba9876543210" * 4
MALFORMED_URL = "http://agent:pw@[bad/receipt.pdf?X-Amz-Signature=" + "9f8e7d6c5b4a3210" * 4
DOWNLOADED_URL = "https://bucket.example.com/statement.pdf?X-Amz-Signature=" + "a1b2c3d4e5f60789" * 4


def _token(url: str) -> str:
    return mask_opaque_urls({"url": url}).mask(url)


@pytest.mark.asyncio
async def test_a_nested_agent_result_gives_page_url_at_start_upload_attempts_recorded_actions_and_closing_note(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    context = app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts[RUN]
    context.parameters |= {
        "fax_file": _file_parameter("fax_file"),
        "notes_file": _file_parameter("notes_file"),
        "login_credential": _credential_parameter(),
    }
    context.values |= {"fax_file": FAX_URL, "notes_file": NOTES_URL}
    working_page = SimpleNamespace(url="about:blank")
    # The fourth call's page read and action read both fail; the taskless children make no action read.
    browser_state = SimpleNamespace(
        get_working_page=AsyncMock(side_effect=[working_page] * 3 + [RuntimeError("page closed")] + [working_page] * 8),
        take_fullpage_screenshot=AsyncMock(return_value=None),
    )
    monkeypatch.setattr(app.BROWSER_MANAGER, "get_for_workflow_run", lambda *_, **__: browser_state)
    read_actions = database.tasks.get_task_actions_hydrated
    action_reads = (
        [read_actions] * 3 + [AsyncMock(side_effect=RuntimeError("database unavailable"))] + [read_actions] * 6
    )
    monkeypatch.setattr(
        database.tasks, "get_task_actions_hydrated", lambda *args, **kwargs: action_reads.pop(0)(*args, **kwargs)
    )

    def uploads(*rows: tuple[str, ActionStatus]) -> list[Action]:
        return [UploadFileAction(element_id="file", file_url=url, status=status) for url, status in rows]

    long_rows = [_clicked(f"Filler {n}") for n in range(32)] + [
        Action(action_type=ActionType.GOTO_URL, status=ActionStatus.failed, description=f"{V3}navigate"),
        GotoUrlAction(url="opaque_url_deadbeef?x=s", status=ActionStatus.completed, description=f"{V3}navigate"),
        _clicked(f"{SECRET} {'x' * 250}"),
    ]
    item_a = {"item": "Physical item A", "price": "48.00"}
    applied = f"Already applied on 16.11 with {SECRET}, so nothing was sent."
    # Each child: its action rows (None gives it no task), its status, its output, the page it leaves and its note.
    children: list[tuple[list[Action] | None, BlockStatus, dict[str, Any] | None, str, str | None]] = [
        (
            uploads((_token(FAX_URL), ActionStatus.completed), (NOTES_URL, ActionStatus.completed)),
            BlockStatus.completed,
            None,
            "https://agent:pw@example.com/upload?session_id=sK8pL2nQ5vR9xT3mW6yZ1aBc#done",
            None,
        ),
        (
            uploads(
                (_token(DECOY_URL), ActionStatus.completed),
                (MALFORMED_URL, ActionStatus.failed),
                ("https://example.com/other.pdf?token=abc123", ActionStatus.failed),
            ),
            BlockStatus.completed,
            {"downloaded_file_urls": [DOWNLOADED_URL]},
            MALFORMED_URL,
            "",
        ),
        (uploads((_token(DOWNLOADED_URL), ActionStatus.completed)), BlockStatus.timed_out, None, FORM_URL, None),
        (uploads((_token(FAX_URL), ActionStatus.completed)), BlockStatus.completed, None, FORM_URL, "Uploaded."),
        (_shopped("Physical item A"), BlockStatus.completed, item_a, FORM_URL, None),
        (_shopped("Physical item B"), BlockStatus.completed, item_a, FORM_URL, None),
        (long_rows, BlockStatus.completed, item_a, FORM_URL, applied),
        ([], BlockStatus.failed, item_a, FORM_URL, "Could not find the item."),
        (
            [_clicked("Physical item A"), _clicked("Physical item B", description=None)],
            BlockStatus.completed,
            item_a,
            FORM_URL,
            None,
        ),
        (None, BlockStatus.completed, item_a, FORM_URL, None),
        (_shopped("Physical item D"), BlockStatus.completed, item_a, FORM_URL, "Saved statement.pdf."),
        (None, BlockStatus.completed, None, FORM_URL, "Signed in as the portal admin."),
    ]

    async def nested_agent(
        block: BaseTaskBlock, workflow_run_id: str, workflow_run_block_id: str, **_: object
    ) -> BlockResult:
        rows, status, output, page_after, note = children.pop(0)
        if note is not None:
            await database.observer.update_workflow_run_block(
                workflow_run_block_id=workflow_run_block_id, organization_id=ORG, finish_reason=note
            )
        if rows is not None:
            await _child_task(database, workflow_run_block_id, rows)
            await _child_task(database, None, [_clicked("Physical item C")])
        if output is not None:
            await block.record_output_parameter_value(context, RUN, output)
        working_page.url = page_after
        return BlockResult(
            success=status == BlockStatus.completed,
            output_parameter=block.output_parameter,
            output_parameter_value=output,
            status=status,
            workflow_run_block_id=workflow_run_block_id,
        )

    monkeypatch.setattr(BaseTaskBlock, "execute", nested_agent)
    upload_calls = ("upload", "read", "again", "broken")
    action_calls = ("a", "b", "long", "none", "legacy", "taskless")
    model = _ScriptedModel(
        [
            [
                scripted_call(
                    "browser_task", {**UPLOAD, "url": FORM_URL, "files": ["fax_file", "notes_file"]}, call_id="upload"
                )
            ],
            [scripted_call("browser_task", {"url": FORM_URL, "navigation_goal": "Read the receipt"}, call_id="read")],
            [
                scripted_call(
                    "browser_task", {"navigation_goal": "Upload it", "files": ["agent_task_2_output"]}, call_id="again"
                )
            ],
            [scripted_call("browser_task", {**UPLOAD, "url": FORM_URL, "files": ["fax_file"]}, call_id="broken")],
            *(
                [scripted_call("browser_task", {"navigation_goal": "Buy one item"}, call_id=call)]
                for call in action_calls
            ),
            [
                scripted_call(
                    "run_block",
                    {"block_type": "file_download", "arguments": {"url": FORM_URL, "navigation_goal": "Get it"}},
                    call_id="download",
                )
            ],
            [scripted_call("login", {"credential": "login_credential", "url": FORM_URL}, call_id="login")],
            [scripted_call("read_run_data", {"name": "agent_task_5"}, call_id="reread_a")],
            [scripted_call("read_run_data", {"name": "agent_task_7"}, call_id="reread_long")],
            VALID_FINISH,
        ]
    )

    result, _ = await _run_agent_block(
        monkeypatch,
        LUNA_MEDIUM,
        model,
        tools=["browser_task", "file_download", "login"],
        parameter_keys=["login_credential"],
    )

    calls = (*upload_calls, *action_calls, "download", "login")
    texts = {call: tool_outputs_for(model.inputs[-1][1], call)[0] for call in (*calls, "reread_a", "reread_long")}
    reports = {call: json.loads(text) for call, text in texts.items()}
    facts = [
        {key: reports[call][key] for key in ("page_url_at_start", "upload_attempts") if key in reports[call]}
        for call in upload_calls
    ]
    assert facts == [
        {
            "page_url_at_start": "about:blank",
            "upload_attempts": [
                {"parameter": "fax_file", "status": "completed"},
                {"parameter": "notes_file", "status": "completed"},
            ],
        },
        {
            "page_url_at_start": FORM_URL,
            "upload_attempts": [
                {"source": _token(DECOY_URL), "status": "completed"},
                {"source": "unparseable url", "status": "failed"},
                {"source": "https://example.com/other.pdf", "status": "failed"},
            ],
        },
        {
            "page_url_at_start": "unparseable url",
            "upload_attempts": [{"parameter": "agent_task_2_output", "status": "completed"}],
        },
        {},
    ]
    a, b = reports["a"], reports["b"]
    assert {**a, "recorded_actions": None} == {**b, "recorded_actions": None}
    assert a["recorded_actions"] == [
        {"action": "navigate", "url": "https://shop.example.com/items", "status": "completed"},
        {"action": 'Clicked the "Physical item A" link', "status": "completed"},
        {"action": "complete", "status": "completed"},
    ]
    assert b["recorded_actions"][1] == {"action": 'Clicked the "Physical item B" link', "status": "completed"}
    assert reports["download"]["recorded_actions"][1] == {
        "action": 'Clicked the "Physical item D" link',
        "status": "completed",
    }
    assert texts["reread_a"] == texts["a"]
    assert texts["reread_long"] == texts["long"]
    long_actions = reports["long"]["recorded_actions"]
    assert len(long_actions) == 35
    assert long_actions[0]["action"] == 'Clicked the "Filler 0" link'
    assert long_actions[-3:] == [
        {"action": "navigate", "status": "failed"},
        {"action": "navigate", "url": "unparseable url", "status": "completed"},
        {"action": f'Clicked the "***** {"x" * 250}" link', "status": "completed"},
    ]
    assert reports["none"]["recorded_actions"] == []
    assert list(reports["long"]) == [
        "status",
        "page_url_at_start",
        "upload_attempts",
        "recorded_actions",
        "finish_reason",
        "output",
        "error",
    ]
    notes = {call: reports[call]["finish_reason"] for call in calls if "finish_reason" in reports[call]}
    assert notes == {
        "broken": "Uploaded.",
        "long": "Already applied on 16.11 with *****, so nothing was sent.",
        "download": "Saved statement.pdf.",
        "login": "Signed in as the portal admin.",
    }
    assert [reports[call]["status"] for call in ("none", "login")] == ["failed", "completed"]
    assert [call for call in calls if "recorded_actions" in reports[call]] == ["a", "b", "long", "none", "download"]
    assert children == []
    assert result.status == BlockStatus.completed
    assert isinstance(result.output_parameter_value, dict)
    tool_calls = result.output_parameter_value["tool_calls"]
    assert [call["status"] for call in tool_calls[:4]] == ["completed", "completed", "timed_out", "completed"]
    assert [{key: call[key] for key in facts[0] if key in call} for call in tool_calls[:4]] == facts
    assert [call.get("recorded_actions") for call in tool_calls[4:6]] == [a["recorded_actions"], b["recorded_actions"]]
    assert [call["status"] for call in tool_calls[: len(calls)]] == [reports[call]["status"] for call in calls]
    assert not [call for call in tool_calls if "finish_reason" in call]
    block_output = json.dumps(result.output_parameter_value)
    url_leaks = (FAX_URL, DECOY_URL, DOWNLOADED_URL, "agent:pw", "9f8e7d6c5b4a3210", "sK8pL2nQ5vR9", "abc123")
    assert not [leak for leak in url_leaks if leak in block_output]
    every_text = json.dumps(texts) + block_output
    assert not [leak for leak in (SECRET, "Physical item C", "Physical item Z", "sK8pL2") if leak in every_text]


SHOP_URL = "https://shop.example.com/items?session=sK8pL2nQ5vR9xT3mW6yZ1aBc#top"
V3 = TASK_V3_ACTION_DESCRIPTION_PREFIX


async def _child_task(database: AgentDB, workflow_run_block_id: str | None, rows: list[Action]) -> None:
    task = await database.tasks.create_task(
        url="",
        title=None,
        navigation_goal=None,
        data_extraction_goal=None,
        navigation_payload=None,
        organization_id=ORG,
        workflow_run_id=RUN,
    )
    if workflow_run_block_id is not None:
        await database.observer.update_workflow_run_block(
            workflow_run_block_id=workflow_run_block_id, organization_id=ORG, task_id=task.task_id
        )
    step = await database.tasks.create_step(
        task_id=task.task_id, order=0, retry_index=0, organization_id=ORG, status=StepStatus.running
    )
    for order, row in enumerate(rows):
        await database.workflow_params.create_action(
            row.model_copy(
                update={
                    "organization_id": ORG,
                    "workflow_run_id": RUN,
                    "task_id": task.task_id,
                    "step_id": step.step_id,
                    "step_order": 0,
                    "action_order": order,
                }
            )
        )


def _clicked(target: str, description: str | None = f"{V3}click e12") -> Action:
    return Action(
        action_type=ActionType.CLICK,
        status=ActionStatus.completed,
        element_id="e12",
        description=description,
        intention=f'Clicked the "{target}" link',
        reasoning="Turn reasoning that names Physical item Z.",
    )


def _shopped(item: str) -> list[Action]:
    return [
        GotoUrlAction(
            url=SHOP_URL,
            status=ActionStatus.completed,
            description=f"{V3}navigate",
            intention=f"Navigated to {SHOP_URL}",
        ),
        _clicked(item),
        Action(action_type=ActionType.COMPLETE, status=ActionStatus.completed, description=f"{V3}finish completed"),
    ]


EMAIL = {"sender": "agent@example.com", "subject": "Report", "body": "Done."}


@pytest.mark.asyncio
@pytest.mark.usefixtures("offline_address_validation")
@pytest.mark.parametrize("scope_platform_smtp", [False, True])
@pytest.mark.parametrize("alias", [False, True])
async def test_static_platform_email_and_unrelated_agent_run_without_exposing_smtp(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, page: ScriptSkyvernPage, scope_platform_smtp: bool, alias: bool
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    monkeypatch.setattr(
        app.AGENT_FUNCTION, "platform_smtp_scoping_required", AsyncMock(return_value=scope_platform_smtp)
    )
    enforce = AsyncMock(side_effect=PermissionError("credential not accessible") if scope_platform_smtp else None)
    monkeypatch.setattr(app.AGENT_FUNCTION, "enforce_workflow_context_credential_use", enforce)
    monkeypatch.setattr(WorkflowRunContext, "_should_include_secrets_in_templates", AsyncMock(return_value=False))
    workflow = _make_workflow([])
    secret_key = "mail_password" if alias else "smtp_password"
    parameters = (
        [AWSSecretParameterYAML(key=secret_key, aws_key=PLATFORM_SMTP_AWS_KEYS["smtp_password"])] if alias else []
    )
    static_email = _platform_email()
    if alias:
        static_email = static_email.model_copy(update={"smtp_password_secret_parameter_key": secret_key})
    workflow.workflow_definition = convert_workflow_definition(
        WorkflowDefinitionYAML(
            parameters=parameters,
            workflow_system_prompt="Rules: {{ incoming }}",
            blocks=[static_email, AgentStepBlockYAML(label="agent", prompt="Read {{ incoming }}")],
        ),
        workflow_id=workflow.workflow_id,
    )
    platform_values = dict(
        zip(PLATFORM_SMTP_AWS_KEYS.values(), ("smtp.example.com", "587", "sender-user", "smtp-password"), strict=True)
    )
    aws_client = MagicMock(get_secret=AsyncMock(side_effect=platform_values.get))
    secrets = [p for p in workflow.workflow_definition.parameters if isinstance(p, AWSSecretParameter)]
    context = await _initialize_smtp_workflow(workflow, secrets, aws_client)
    context.workflow_run_id = RUN
    context.organization_id = ORG
    app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts[RUN] = context
    placeholder = context.values[secret_key]
    original_values = {key: context.values[key] for key in context.platform_smtp_parameter_keys()}
    context.values["incoming"] = {
        secret_key: placeholder,
        "text": f"prefix {placeholder} suffix",
        "literal": "smtp-password",
    }
    context.workflow_run_outputs["snapshot"] = {
        secret_key: placeholder,
        "nested": {"token": placeholder},
        "snapshot_text": json.dumps({secret_key: placeholder}),
        "plaintext_snapshot": json.dumps({secret_key: "smtp-password"}),
        "count": 1587,
    }
    monkeypatch.setattr(
        app, "STORAGE", SimpleNamespace(get_current_attempt_downloaded_files=AsyncMock(return_value=[]))
    )
    child_system_prompts: list[str | None] = []

    async def child_llm(prompt: str, prompt_name: str, system_prompt: str | None = None, **_: object) -> dict[str, str]:
        if prompt_name == "generate-workflow-run-block-description":
            return {"summary": "description"}
        child_system_prompts.append(system_prompt)
        return {"llm_response": "done"}

    async def extract(prompt: str, schema: dict[str, Any] | None = None, **kwargs: str | None) -> str:
        child_system_prompts.append(kwargs.get("system_prompt", context.resolve_effective_workflow_system_prompt()))
        return "done"

    monkeypatch.setattr(app, "SECONDARY_LLM_API_HANDLER", child_llm)
    page.extract.side_effect = extract
    model = _ScriptedModel(
        [
            [scripted_call("read_run_data", {}, call_id="index")],
            [scripted_call("read_run_data", {"name": "snapshot"}, call_id="snapshot")],
            [
                scripted_call(
                    "run_block", {"block_type": "text_prompt", "arguments": {"prompt": "Continue"}}, call_id="text"
                )
            ],
            [scripted_call("page_extract", {"prompt": "the title"}, call_id="extract")],
            [scripted_call("run_block", {"block_type": "wait", "arguments": {"wait_sec": 1}}, call_id="wait")],
            [
                scripted_call(
                    "run_block",
                    {"block_type": "send_email", "arguments": {**EMAIL, "recipients": ["ops@example.com"]}},
                    call_id="email",
                )
            ],
            [scripted_call("finish", {"answer": "done"}, call_id="finish")],
        ]
    )

    with (
        skyvern_context.scoped(SkyvernContext(workflow_run_id=RUN, organization_id=ORG)),
        patch("smtplib.SMTP") as smtp,
    ):
        result, _ = await _run_agent_block(monkeypatch, LUNA_MEDIUM, model, prompt="Read {{ incoming }}")
        static_result = await workflow.workflow_definition.blocks[0].execute_safe(
            workflow_run_id=RUN, organization_id=ORG
        )

    assert result.status == BlockStatus.completed
    assert static_result.status == BlockStatus.completed
    assert smtp.call_count == 2
    assert {key: context.values[key] for key in original_values} == original_values
    assert not set(original_values) & set(_tool_output(model, "index")["parameters"])
    assert _tool_output(model, "snapshot")["output"]["count"] == 1587
    exposed = json.dumps(model.inputs, default=str)
    assert not any(value in exposed for value in original_values.values())
    assert "smtp-password" not in exposed
    assert len(child_system_prompts) == 2
    assert placeholder not in json.dumps(child_system_prompts)
    assert "smtp-password" not in json.dumps(child_system_prompts)
    enforce.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["smtp_password", "mail_password"])
async def test_agent_tool_cannot_read_platform_smtp_parameters(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, name: str
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    context = app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts[RUN]
    now = datetime.now(UTC)
    parameter = AWSSecretParameter(
        key=name,
        aws_secret_parameter_id="asp_test",
        workflow_id="w_1",
        aws_key=PLATFORM_SMTP_AWS_KEYS["smtp_password"],
        created_at=now,
        modified_at=now,
    )
    placeholder = context.register_secret_value("platform-password")
    context.parameters[name] = parameter
    context.values[name] = placeholder
    context._platform_smtp_secret_ids[placeholder] = parameter.aws_key
    model = _ScriptedModel([[scripted_call("read_run_data", {"name": name}, call_id="read")]])

    result, _ = await _run_agent_block(monkeypatch, LUNA_MEDIUM, model)

    assert result.status == BlockStatus.failed
    assert placeholder not in json.dumps(model.inputs, default=str)
    assert "platform-password" not in json.dumps(model.inputs, default=str)


def _register_platform_parameter(context: WorkflowRunContext, key: str, field: str, secret: str) -> str:
    now = datetime.now(UTC)
    parameter = AWSSecretParameter(
        key=key,
        aws_secret_parameter_id="asp_test",
        workflow_id="w_1",
        aws_key=PLATFORM_SMTP_AWS_KEYS[field],
        created_at=now,
        modified_at=now,
    )
    placeholder = context.register_secret_value(secret)
    context.parameters[key] = parameter
    context.values[key] = placeholder
    context._platform_smtp_secret_ids[placeholder] = parameter.aws_key
    return placeholder


@pytest.mark.asyncio
@pytest.mark.parametrize("port_value", ["587", 587])
@pytest.mark.parametrize("short_secret", ["abc", "abcdefg"])
async def test_smtp_port_does_not_mask_unrelated_agent_values(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, port_value: str | int, short_secret: str
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    context = app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts[RUN]
    port_placeholder = _register_platform_parameter(context, "smtp_port", "smtp_port", "587")
    context.secrets[port_placeholder] = port_value
    _register_platform_parameter(context, "short_password", "smtp_password", short_secret)
    context.values.update(amount=587, ordinary=short_secret)
    context.inherited_workflow_system_prompt = "System {{ ordinary | upper }}"
    context.workflow_run_outputs["snapshot"] = {
        "amount": 587,
        "port_text": "587",
        "ordinary": short_secret,
        "uppercase": short_secret.upper(),
    }
    model = _ScriptedModel(
        [
            [scripted_call("read_run_data", {"name": "snapshot"}, call_id="snapshot")],
            [scripted_call("finish", {"answer": "done"}, call_id="finish")],
        ]
    )

    with skyvern_context.scoped(SkyvernContext(workflow_run_id=RUN, organization_id=ORG)):
        previous = TaskBlock(
            label="previous", output_parameter=make_block_output_parameter("previous_output"), title=""
        )
        assert (
            previous.format_block_parameter_template_from_workflow_run_context("{{ ordinary | upper }}", context)
            == short_secret.upper()
        )
        assert script_service.render_template("{{ ordinary | upper }}") == short_secret.upper()
        assert real_skyvern_page_ai.render_template("{{ ordinary | upper }}") == short_secret.upper()
        result, _ = await _run_agent_block(
            monkeypatch,
            LUNA_MEDIUM,
            model,
            prompt="Amount {{ amount }}, next {{ amount + 1 }}, literal 587, {{ ordinary }}, {{ ordinary | upper }}",
        )

    assert result.status == BlockStatus.completed
    assert f"Amount 587, next 588, literal 587, {short_secret}, {short_secret.upper()}" in str(model.inputs[0][1])
    assert f"System {short_secret.upper()}" in str(model.inputs[0][0])
    assert _tool_output(model, "snapshot")["output"] == {
        "amount": 587,
        "port_text": "587",
        "ordinary": short_secret,
        "uppercase": short_secret.upper(),
    }


@pytest.mark.asyncio
async def test_platform_alias_does_not_remove_nested_customer_keys(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    context = app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts[RUN]
    placeholder = _register_platform_parameter(context, "status", "smtp_password", "platform-password")
    context.values["incoming"] = {"status": "ready"}
    context.workflow_run_outputs["snapshot"] = {
        "status": "customer-status",
        "nested": {"status": "ready", "token": placeholder},
    }
    monkeypatch.setattr(
        app, "STORAGE", SimpleNamespace(get_current_attempt_downloaded_files=AsyncMock(return_value=[]))
    )
    model = _ScriptedModel(
        [
            [scripted_call("read_run_data", {}, call_id="index")],
            [scripted_call("read_run_data", {"name": "snapshot"}, call_id="snapshot")],
            [scripted_call("finish", {"answer": "done"}, call_id="finish")],
        ]
    )

    result, _ = await _run_agent_block(monkeypatch, LUNA_MEDIUM, model, prompt="Use {{ incoming.status }}")

    assert result.status == BlockStatus.completed
    assert "Use ready" in str(model.inputs[0][1])
    assert "status" not in _tool_output(model, "index")["parameters"]
    (text,) = tool_outputs_for(model.inputs[-1][1], "snapshot")
    report = json.loads(text)
    assert report["status"] == "completed"
    assert report["output"]["output"] == {"status": "customer-status", "nested": {"status": "ready", "token": "*****"}}
    assert placeholder not in text


@pytest.mark.asyncio
async def test_agent_browser_task_filters_conditional_snapshot_from_error_mapping(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    monkeypatch.setattr(WorkflowRunContext, "_should_include_secrets_in_templates", AsyncMock(return_value=False))
    workflow = _make_workflow([])
    workflow.workflow_definition = convert_workflow_definition(
        WorkflowDefinitionYAML(
            parameters=[],
            error_code_mapping={
                "SNAPSHOT": '{{ decision_output.llm_prompt.split("Use this context to understand variable values:")[1] }}',
                "PORT_TEXT": "Port 587 is ordinary.",
            },
            blocks=[
                _platform_email(),
                ConditionalBlockYAML(
                    label="decision",
                    branch_conditions=[
                        BranchConditionYAML(
                            criteria=BranchCriteriaYAML(criteria_type="prompt", expression="Continue?")
                        ),
                        BranchConditionYAML(is_default=True),
                    ],
                ),
                AgentStepBlockYAML(label="agent", prompt="Continue", tools=["browser_task"]),
            ],
        ),
        workflow_id=workflow.workflow_id,
    )
    platform_values = dict(
        zip(PLATFORM_SMTP_AWS_KEYS.values(), ("smtp.example.com", "587", "sender-user", "smtp-password"), strict=True)
    )
    aws_client = MagicMock(get_secret=AsyncMock(side_effect=platform_values.get))
    secrets = [p for p in workflow.workflow_definition.parameters if isinstance(p, AWSSecretParameter)]
    context = await _initialize_smtp_workflow(workflow, secrets, aws_client)
    context.workflow_run_id = RUN
    context.organization_id = ORG
    context.values["ordinary"] = "visible data"
    app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts[RUN] = context
    placeholders = {context.values[key] for key in PLATFORM_SMTP_AWS_KEYS}

    async def evaluate(extraction: ExtractionBlock, **_: object) -> BlockResult:
        return BlockResult(
            success=True,
            output_parameter=extraction.output_parameter,
            output_parameter_value={"evaluations": [{"condition_index": 1, "reasoning": "ok", "result": True}]},
        )

    mappings: list[dict[str, str] | None] = []

    async def capture_task(
        block: TaskBlock, workflow_run_id: str, workflow_run_block_id: str, **_: object
    ) -> BlockResult:
        block.format_potential_template_parameters(context)
        mappings.append(block.error_code_mapping)
        return BlockResult(success=True, output_parameter=block.output_parameter, output_parameter_value={})

    monkeypatch.setattr(ExtractionBlock, "execute", evaluate)
    monkeypatch.setattr(TaskBlock, "execute", capture_task)
    model = _ScriptedModel(
        [
            [
                scripted_call(
                    "browser_task", {"url": "https://example.com", "navigation_goal": "Continue"}, call_id="task"
                )
            ],
            [scripted_call("finish", {"answer": "done"}, call_id="finish")],
        ]
    )
    conditional = workflow.workflow_definition.blocks[1]
    assert isinstance(conditional, ConditionalBlock)

    with skyvern_context.scoped(SkyvernContext(workflow_run_id=RUN, organization_id=ORG)):
        await conditional.execute_safe(workflow_run_id=RUN, organization_id=ORG)
        assert any(token in context.values["decision_output"]["llm_prompt"] for token in placeholders)
        result, _ = await _run_agent_block(monkeypatch, LUNA_MEDIUM, model, tools=["browser_task"])

    assert result.status == BlockStatus.completed
    assert len(mappings) == 1 and mappings[0] is not None
    assert "visible data" in mappings[0]["SNAPSHOT"]
    assert mappings[0]["PORT_TEXT"] == "Port 587 is ordinary."
    assert all(token not in json.dumps(mappings) for token in placeholders)


@pytest.mark.asyncio
@pytest.mark.usefixtures("offline_address_validation")
@pytest.mark.parametrize("scope_platform_smtp", [False, True])
async def test_a_send_email_child_sends_through_the_platform_smtp_secrets(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, scope_platform_smtp: bool
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    monkeypatch.setattr(
        app.AGENT_FUNCTION, "platform_smtp_scoping_required", AsyncMock(return_value=scope_platform_smtp)
    )
    platform_secrets = {
        PLATFORM_SMTP_AWS_KEYS["smtp_host"]: "smtp.example.com",
        PLATFORM_SMTP_AWS_KEYS["smtp_port"]: "587",
        PLATFORM_SMTP_AWS_KEYS["smtp_username"]: "skyvern",
        PLATFORM_SMTP_AWS_KEYS["smtp_password"]: "smtp-password",
    }
    app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts[RUN]._aws_client.get_secret.side_effect = platform_secrets.get
    email = {**EMAIL, "recipients": ["ops@example.com"]}
    model = _ScriptedModel(
        [
            [scripted_call("run_block", {"block_type": "send_email", "arguments": email}, call_id="c1")],
            [scripted_call("run_block", {"block_type": "send_email", "arguments": email}, call_id="c2")],
            [scripted_call("finish", {"answer": "sent"}, call_id="c3")],
        ]
    )

    with patch("smtplib.SMTP") as smtp:
        await _run_agent_block(monkeypatch, LUNA_MEDIUM, model, prompt="Email the report to the address on the page.")

    rows = await database.observer.get_workflow_run_blocks(workflow_run_id=RUN, organization_id=ORG)
    children = [(row.block_type, row.status) for row in rows if row.parent_workflow_run_block_id]
    assert children == [(BlockType.SEND_EMAIL, "completed")] * 2
    retained = set(PLATFORM_SMTP_AWS_KEYS) & app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts[RUN].parameters.keys()
    assert not retained
    context = app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts[RUN]
    assert context._platform_smtp_secret_ids
    for placeholder in context._platform_smtp_secret_ids:
        with pytest.raises(InvalidWorkflowDefinition):
            context.get_original_secret_value_or_none(placeholder)
    assert smtp.call_count == 2
    assert smtp.call_args.args[0] == "smtp.example.com"
    assert "smtp-password" not in json.dumps(model.inputs, default=str)


@pytest.mark.asyncio
@pytest.mark.usefixtures("offline_address_validation")
@pytest.mark.parametrize("preexisting", [False, True])
async def test_agent_email_child_cannot_use_platform_smtp_as_its_body(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, preexisting: bool
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    monkeypatch.setattr(app.AGENT_FUNCTION, "platform_smtp_scoping_required", AsyncMock(return_value=False))
    platform_secrets = {
        PLATFORM_SMTP_AWS_KEYS["smtp_host"]: "smtp.example.com",
        PLATFORM_SMTP_AWS_KEYS["smtp_port"]: "587",
        PLATFORM_SMTP_AWS_KEYS["smtp_username"]: "skyvern",
        PLATFORM_SMTP_AWS_KEYS["smtp_password"]: "smtp-password",
    }
    context = app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts[RUN]
    context._aws_client.get_secret.side_effect = platform_secrets.get
    email = {**EMAIL, "recipients": ["ops@example.com"]}
    turns = []
    if preexisting:
        parameters = {"seed_output": make_block_output_parameter("seed_output")}
        block_yaml_to_block(SendEmailBlockYAML(label="seed", **email), parameters)
        for parameter in parameters.values():
            if isinstance(parameter, AWSSecretParameter):
                placeholder = context.generate_random_secret_id()
                context.parameters[parameter.key] = parameter
                context.values[parameter.key] = placeholder
                context.secrets[placeholder] = platform_secrets[parameter.aws_key]
    else:
        turns.append([scripted_call("run_block", {"block_type": "send_email", "arguments": email}, call_id="c1")])
    turns.append(
        [
            scripted_call(
                "run_block",
                {"block_type": "send_email", "arguments": {**email, "body": "smtp_password"}},
                call_id="c2",
            )
        ]
    )
    model = _ScriptedModel(turns)

    with patch("smtplib.SMTP") as smtp:
        result, _ = await _run_agent_block(monkeypatch, LUNA_MEDIUM, model)

    assert result.status == BlockStatus.failed
    assert smtp.call_count == (0 if preexisting else 1)
    if preexisting:
        assert set(PLATFORM_SMTP_AWS_KEYS).issubset(context.parameters)
    else:
        assert not set(PLATFORM_SMTP_AWS_KEYS) & context.parameters.keys()
    assert "smtp-password" not in json.dumps(model.inputs, default=str)


@pytest.mark.asyncio
@pytest.mark.parametrize("ignore", [False, True])
async def test_the_agent_and_what_it_runs_follow_the_workflow_system_prompt_unless_the_block_opts_out(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, page: ScriptSkyvernPage, ignore: bool
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    run_context = app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts[RUN]
    run_context.inherited_workflow_system_prompt = "Answer in French."
    sent: list[str | None] = []

    async def fake_llm(prompt: str, prompt_name: str, system_prompt: str | None = None, **_: object) -> dict[str, str]:
        if prompt_name == "text-prompt":
            sent.append(system_prompt)
        return {"llm_response": "oui"}

    async def extract(prompt: str, schema: dict[str, Any] | None = None, **kwargs: str | None) -> str:
        # Page AI that no block label is bound to resolves an omitted system prompt to the run-wide one.
        sent.append(kwargs.get("system_prompt", run_context.resolve_effective_workflow_system_prompt()))
        return "oui"

    monkeypatch.setattr(app, "SECONDARY_LLM_API_HANDLER", fake_llm)
    page.extract.side_effect = extract
    model = _ScriptedModel(
        [
            [
                scripted_call(
                    "run_block", {"block_type": "text_prompt", "arguments": {"prompt": "Say hi."}}, call_id="c1"
                )
            ],
            [scripted_call("page_extract", {"prompt": "the title"}, call_id="c2")],
            [scripted_call("finish", {"answer": "oui"}, call_id="c3")],
        ]
    )

    with skyvern_context.scoped(SkyvernContext(workflow_run_id=RUN, organization_id=ORG)):
        await _run_agent_block(monkeypatch, LUNA_MEDIUM, model, ignore_workflow_system_prompt=ignore)

    assert ("Answer in French." in str(model.inputs[0][0])) is not ignore
    assert sent == [None if ignore else "Answer in French."] * 2


def _tool_output(model: _ScriptedModel, call_id: str) -> Any:
    (text,) = tool_outputs_for(model.inputs[-1][1], call_id)
    return json.loads(text)["output"]


@pytest.mark.asyncio
async def test_read_run_data_returns_an_earlier_blocks_output_and_masks_secret_parameters(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    run_context = app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts[RUN]
    now = datetime.now(UTC)
    run_context.parameters["api_token"] = AWSSecretParameter(
        key="api_token",
        aws_secret_parameter_id="asp_1",
        workflow_id="w_1",
        aws_key="api-token",
        created_at=now,
        modified_at=now,
    )
    run_context.register_block_reference_variable("api_token", {"total": 1}, carried=True)
    # A secret parameter's value is the id its secret is registered under, not the secret; registering it overwrites a
    # carried block output of the same name.
    run_context.values["api_token"] = "secret_id_api_token"
    run_context.secrets["secret_id_api_token"] = SECRET
    await run_context.register_output_parameter_value_post_execution(
        make_block_output_parameter("search_output"), {"extracted_information": {"price": 42, "token": SECRET}}
    )
    run_context.record_block_outcome("search", BlockStatus.completed, [], None)
    quote = {"total": 7, "token": SECRET}
    run_context.values["quote_output"] = quote
    run_context.register_block_reference_variable("quote", quote, carried=True)
    category = [{"category": "UNKNOWN", "confidence_float": 0.5, "reasoning": "No keyword match found"}]
    authored = [
        {"failure_category": []},
        {"failure_category": ["late"]},
        {"failure_category": [{"code": "late_delivery", "count": 3}]},
    ]
    records = [*authored, {"failure_category": None}, {"failure_category": [{"reason_code": "site_throttle"}]}]
    loop_output = [
        [
            {
                "loop_value": "a",
                "output_parameter": None,
                "output_value": {"failure_category": category, "records": records},
            }
        ]
    ]
    await run_context.register_output_parameter_value_post_execution(
        make_block_output_parameter("loop_output"), loop_output
    )
    run_context.record_block_outcome("loop", BlockStatus.completed, [], None)
    child_output = {"status": "failed", "failure_category": category, "errors": [{"error_code": "page_closed"}]}

    async def failed_task(
        block: BaseTaskBlock, workflow_run_id: str, workflow_run_block_id: str, **_: object
    ) -> BlockResult:
        await block.record_output_parameter_value(run_context, RUN, child_output)
        return BlockResult(
            success=False,
            output_parameter=block.output_parameter,
            output_parameter_value=child_output,
            status=BlockStatus.failed,
            failure_reason="page closed",
            workflow_run_block_id=workflow_run_block_id,
        )

    monkeypatch.setattr(BaseTaskBlock, "execute", failed_task)

    async def downloaded_files(organization_id: str, run_id: str | None) -> list[FileInfo]:
        return [FileInfo(url="https://example.com/invoice.pdf", filename="invoice.pdf")]

    monkeypatch.setattr(app, "STORAGE", SimpleNamespace(get_current_attempt_downloaded_files=downloaded_files))
    reads = ("search", "search_output", "quote", "quote_output", "loop", "api_token", "api_token_output")
    model = _ScriptedModel(
        [
            [
                scripted_call("read_run_data", {}, call_id="index"),
                *(scripted_call("read_run_data", {"name": name}, call_id=name) for name in reads),
            ],
            [scripted_call("browser_task", {"navigation_goal": "Open it"}, call_id="task")],
            [
                scripted_call("read_run_data", {"name": "agent_task_9"}, call_id="reread"),
                scripted_call("read_run_data", {"name": "agent_task_9_output"}, call_id="reread_output"),
            ],
            [scripted_call("finish", {"answer": "42"}, call_id="finish")],
        ]
    )

    result, _ = await _run_agent_block(monkeypatch, LUNA_MEDIUM, model)

    assert result.status == BlockStatus.completed
    assert _tool_output(model, "index") == {
        "parameters": ["api_token"],
        "blocks": {"search": "completed", "loop": "completed", "api_token": None, "quote": None},
        "files": ["invoice.pdf"],
    }
    search = _tool_output(model, "search")
    assert search["output"]["extracted_information"] == {"price": 42, "token": "*****"}
    assert search["outcome"]["status"] == "completed"
    assert _tool_output(model, "search_output") == search
    assert _tool_output(model, "quote") == _tool_output(model, "quote_output")
    assert _tool_output(model, "quote_output") == {"output": {"total": 7, "token": "*****"}, "outcome": None}
    assert _tool_output(model, "loop")["output"][0][0]["output_value"] == {"records": [*authored, {}, {}]}
    assert _tool_output(model, "api_token") == _tool_output(model, "api_token_output") == "*****"
    for call_id in ("task", "reread", "reread_output"):
        (text,) = tool_outputs_for(model.inputs[-1][1], call_id)
        assert "failure_category" not in text
        assert json.loads(text)["output"]["errors"] == [{"error_code": "page_closed"}]
    assert json.loads(tool_outputs_for(model.inputs[-1][1], "task")[0])["error"] == "page closed"
    assert run_context.values["agent_task_9_output"]["failure_category"] == category
    assert run_context.values["loop_output"][0][0]["output_value"]["failure_category"] == category
    assert run_context.workflow_run_outputs["loop"][0][0]["output_value"]["failure_category"] == category
    assert SECRET not in json.dumps(model.inputs, default=str)


_CUT_NOTICE = re.compile(r'read_run_data\(name="([^"]+)", offset=(\d+)\)')
_CHILD_RESULT_CHARS = 97_628


def _big_child_output(monkeypatch: pytest.MonkeyPatch) -> str:
    """Make every text_prompt child answer so its tool result is _CHILD_RESULT_CHARS long, with SECRET past 20,000."""
    empty = json.dumps({"status": "completed", "output": {"llm_response": ""}, "error": None})
    head = "a" * 25_000
    answer = head + SECRET + "b" * (_CHILD_RESULT_CHARS - len(empty) - len(head) - len("*****"))

    async def fake_llm(prompt: str, prompt_name: str, **_: object) -> dict[str, str]:
        if prompt_name == "generate-workflow-run-block-description":
            return {"summary": "description"}
        return {"llm_response": answer}

    monkeypatch.setattr(app, "SECONDARY_LLM_API_HANDLER", fake_llm)
    return json.dumps({"status": "completed", "output": {"llm_response": answer}, "error": None}).replace(
        SECRET, "*****"
    )


def _next_window(call_id: str) -> Callable[[list[Any]], list[TResponseOutputItem]]:
    def follow(model_input: list[Any]) -> list[TResponseOutputItem]:
        (text,) = tool_outputs_for(model_input, "child")
        name, offset = _CUT_NOTICE.findall(text)[-1]
        return [scripted_call("read_run_data", {"name": name, "offset": int(offset)}, call_id=call_id)]

    return follow


TEXT_PROMPT_CHILD = [
    scripted_call("run_block", {"block_type": "text_prompt", "arguments": {"prompt": "Read it all"}}, call_id="child")
]


@pytest.mark.asyncio
async def test_a_cut_child_result_names_the_read_run_data_call_that_continues_it_exactly(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    report_text = _big_child_output(monkeypatch)
    assert len(report_text) == _CHILD_RESULT_CHARS
    model = _ScriptedModel([TEXT_PROMPT_CHILD, _next_window("second"), VALID_FINISH])

    result, _ = await _run_agent_block(monkeypatch, LUNA_MEDIUM, model)

    assert result.status == BlockStatus.completed
    assert isinstance(result.output_parameter_value, dict)
    label = result.output_parameter_value["tool_calls"][0]["label"]
    (first,) = tool_outputs_for(model.inputs[1][1], "child")
    assert first.startswith(report_text[:20_000])
    notice = first[20_000:]
    assert str(_CHILD_RESULT_CHARS) in notice
    assert f'read_run_data(name="{label}", offset=20000)' in notice
    (second,) = tool_outputs_for(model.inputs[2][1], "second")
    assert first[:20_000] + second[:20_000] == report_text[:40_000]
    assert "*****" in second[:20_000]
    assert f'read_run_data(name="{label}", offset=40000)' in second[20_000:]
    assert SECRET not in json.dumps(model.inputs, default=str)


@pytest.mark.asyncio
async def test_bad_read_run_data_reads_fail_within_the_cap_and_page_results_arrive_unescaped_and_pageable_by_name(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    _big_child_output(monkeypatch)
    japanese = "献立の説明文" * 2_500
    extracts = {"everything": "p" * 30_000, "menu": japanese + "\ud800\u2028\U000e0041"}

    class Page:
        page = SimpleNamespace(url="https://example.com/")
        page_ai = _page_ai("https://example.com/")

        async def extract(self, prompt: str, schema: dict[str, Any] | None = None) -> str:
            return extracts["menu" if "menu" in prompt else "everything"]

    async def create(browser_session_id: str | None = None) -> Page:
        return Page()

    monkeypatch.setattr(ScriptSkyvernPage, "create", create)

    def bad_reads(model_input: list[Any]) -> list[TResponseOutputItem]:
        (text,) = tool_outputs_for(model_input, "child")
        name, _ = _CUT_NOTICE.findall(text)[-1]
        return [
            scripted_call("read_run_data", {"name": name, "offset": _CHILD_RESULT_CHARS}, call_id="past_end"),
            scripted_call("read_run_data", {"name": name, "offset": -1}, call_id="negative"),
            scripted_call("read_run_data", {"name": "x" * 30_000}, call_id="unknown"),
        ]

    def rest_of_page(model_input: list[Any]) -> list[TResponseOutputItem]:
        (text,) = tool_outputs_for(model_input, "page")
        name, offset = _CUT_NOTICE.findall(text)[-1]
        return [scripted_call("read_run_data", {"name": name, "offset": int(offset)}, call_id="rest")]

    model = _ScriptedModel(
        [
            TEXT_PROMPT_CHILD,
            bad_reads,
            [scripted_call("page_extract", {"prompt": "everything"}, call_id="page")],
            rest_of_page,
            [scripted_call("page_extract", {"prompt": "menu"}, call_id="menu")],
            VALID_FINISH,
        ]
    )

    with skyvern_context.scoped(SkyvernContext(workflow_run_id=RUN, organization_id=ORG)):
        await _run_agent_block(monkeypatch, LUNA_MEDIUM, model)

    for call_id in ("past_end", "negative"):
        (text,) = tool_outputs_for(model.inputs[2][1], call_id)
        report = json.loads(text)
        assert (report["status"], report["output"]) == ("failed", None)
        assert f"0 to {_CHILD_RESULT_CHARS - 1}" in report["error"]
    (unknown,) = tool_outputs_for(model.inputs[2][1], "unknown")
    assert unknown.startswith('{"status": "failed", "output": null, "error": "ValueError: Nothing in this run')
    assert len(unknown) < 20_100
    assert "read_run_data(" not in unknown[20_000:]
    url = "https://example.com/"
    page_text = json.dumps({"status": "completed", "url": url, "output": "p" * 30_000, "error": None})
    (page,) = tool_outputs_for(model.inputs[3][1], "page")
    assert page.startswith(page_text[:20_000])
    assert _CUT_NOTICE.findall(page[20_000:]) == [("agent_page_extract_5", "20000")]
    (rest,) = tool_outputs_for(model.inputs[4][1], "rest")
    assert page[:20_000] + rest[: len(page_text) - 20_000] == page_text
    assert "read_run_data(" not in rest
    (menu,) = tool_outputs_for(model.inputs[5][1], "menu")
    assert json.loads(menu) == {"status": "completed", "url": url, "output": extracts["menu"], "error": None}
    assert japanese in menu
    assert menu.encode("utf-8") and not {"\u2028", "\U000e0041"} & set(menu)


@pytest.mark.asyncio
async def test_page_commands_run_under_one_agent_task_reach_page_ai_as_written_and_report_their_page(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, page: ScriptSkyvernPage
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    monkeypatch.setattr(agent_step_loop, "_PAGE_TITLE_SECONDS", 0.01)
    raw_page = cast(MagicMock, page.page)
    raw_page.title = AsyncMock(return_value="Index of /")
    navigations: list[str] = []
    scraped_url = "https://93.184.216.34/service-area/"
    bound: list[tuple[str | None, str | None]] = []
    working_page_errors: list[Exception] = []

    async def get_working_page() -> MagicMock:
        if working_page_errors:
            raise working_page_errors.pop()
        return raw_page

    monkeypatch.setattr(
        app.BROWSER_MANAGER,
        "get_for_workflow_run",
        lambda *_, **__: SimpleNamespace(get_working_page=get_working_page),
    )

    async def navigate(url: str, **_: object) -> None:
        navigations.append(url)
        if url == "https://93.184.216.35/":
            raise PlaywrightTimeoutError("Timeout 60000ms exceeded.")
        raw_page.url = url

    async def title_never_returns() -> str:
        await asyncio.Event().wait()
        return ""

    async def click(**_: object) -> None:
        raw_page.title = title_never_returns

    async def extract(prompt: str, schema: dict[str, Any] | None = None) -> str:
        context = skyvern_context.ensure_context()
        bound.append((context.task_id, context.step_id))
        if "the blank page" in prompt:
            raw_page.url = "about:blank"
            working_page_errors.append(RuntimeError("page closed"))
            raise ScrapingFailedBlankPage()
        page.page_ai.scraped_page = SimpleNamespace(url=scraped_url)
        return _render_template_with_label(prompt)

    raw_page.goto = navigate
    cast(AsyncMock, page.click).side_effect = click
    cast(AsyncMock, page.extract).side_effect = extract
    model = _ScriptedModel(
        [
            [scripted_call("page_goto", {"url": "https://93.184.216.34/"}, call_id="goto")],
            [scripted_call("page_click", {}, call_id="missing")],
            [scripted_call("page_goto", {"url": "https://93.184.216.35/"}, call_id="hung")],
            [scripted_call("page_click", {"prompt": "Next"}, call_id="click")],
            [scripted_call("page_extract", {"prompt": "Echo {{ api_token }}"}, call_id="template")],
            [scripted_call("page_extract", {"prompt": "api_token"}, call_id="key")],
            [scripted_call("page_extract", {"prompt": "the blank page"}, call_id="blank")],
            [scripted_call("page_fill", {"prompt": "Name", "value": "Ada"}, call_id="stale")],
            [scripted_call("finish", {"answer": "done"}, call_id="finish")],
        ]
    )

    with skyvern_context.scoped(SkyvernContext(workflow_run_id=RUN, organization_id=ORG)) as context:
        result, _ = await _run_agent_block(monkeypatch, LUNA_MEDIUM, model)

    assert result.status == BlockStatus.completed
    assert [_tool_output(model, call_id) for call_id in ("template", "key")] == ["Echo {{ api_token }}", "api_token"]
    reports = {
        call_id: json.loads(tool_outputs_for(model.inputs[-1][1], call_id)[0])
        for call_id in ("goto", "missing", "hung", "click", "template", "blank", "stale")
    }
    assert [list(report) for report in reports.values()] == [
        ["status", "url", "title", "output", "error"],
        ["status", "output", "error"],
        ["status", "url", "title", "output", "error"],
        ["status", "url", "output", "error"],
        ["status", "url", "output", "error"],
        ["status", "url", "output", "error"],
        ["status", "output", "error"],
    ]
    assert reports["goto"] == {
        "status": "completed",
        "url": "https://93.184.216.34/",
        "title": "Index of /",
        "output": None,
        "error": None,
    }
    assert navigations == ["https://93.184.216.34/", "https://93.184.216.35/"]
    assert (reports["hung"]["status"], reports["hung"]["error"]) == (
        "failed",
        "TimeoutError: Timeout 60000ms exceeded.",
    )
    assert reports["click"] == {"status": "completed", "url": "https://93.184.216.34/", "output": None, "error": None}
    assert (reports["template"]["url"], reports["template"]["output"]) == (scraped_url, "Echo {{ api_token }}")
    assert reports["blank"] == {
        "status": "failed",
        "url": "about:blank",
        "output": None,
        "error": "ScrapingFailedBlankPage: Scraping failed. It's a blank page. Please ensure there is a non-blank page "
        "for Skyvern to work with.",
    }
    assert (reports["missing"]["status"], reports["stale"]) == (
        "failed",
        {"status": "failed", "output": None, "error": "RuntimeError: page closed"},
    )
    (agent_row,) = await database.observer.get_workflow_run_blocks(workflow_run_id=RUN, organization_id=ORG)
    assert agent_row.task_id
    task = await database.tasks.get_task(agent_row.task_id, organization_id=ORG)
    (step,) = await database.tasks.get_task_steps(agent_row.task_id, organization_id=ORG)
    assert task is not None and (task.status, step.status) == (TaskStatus.completed, StepStatus.completed)
    assert bound == [(task.task_id, step.step_id)] * 3
    assert (context.task_id, context.step_id) == (None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_write", ["step_archive", "llm_cost", "output"])
@pytest.mark.usefixtures("page")
async def test_the_agent_block_bills_its_decisions_when_a_write_after_the_loop_fails(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, billing_hook: AsyncMock, failing_write: str
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    # The block's own failure handling writes its output again, which must succeed.
    failure = AsyncMock(side_effect=[RuntimeError("write failed"), None])
    if failing_write == "step_archive":
        monkeypatch.setattr(app, "ARTIFACT_MANAGER", SimpleNamespace(flush_step_archive=failure))
    elif failing_write == "llm_cost":
        monkeypatch.setattr(database.observer, "increment_workflow_run_block_llm_cost", failure)
    else:
        monkeypatch.setattr(AgentStepBlock, "record_output_parameter_value", failure)
    model = _ScriptedModel(
        [
            [scripted_call("page_extract", {"prompt": "price"}, call_id="extract")],
            [scripted_call("read_run_data", {}, call_id="index")],
            [scripted_call("finish", {"answer": "done"}, call_id="finish")],
        ]
    )

    with skyvern_context.scoped(SkyvernContext(workflow_run_id=RUN, organization_id=ORG)):
        result, _ = await _run_agent_block(monkeypatch, LUNA_MEDIUM, model)

    failure.assert_awaited()
    assert result.status == BlockStatus.failed
    assert _billed_decisions(billing_hook) == 2
    if failing_write != "step_archive":
        (agent_row,) = await database.observer.get_workflow_run_blocks(workflow_run_id=RUN, organization_id=ORG)
        task = await database.tasks.get_task(agent_row.task_id, organization_id=ORG)
        (step,) = await database.tasks.get_task_steps(agent_row.task_id, organization_id=ORG)
        assert task is not None and (task.status, step.status) == (TaskStatus.completed, StepStatus.completed)


@pytest.mark.asyncio
async def test_a_rejected_finish_names_where_its_fields_went_and_no_call_runs_after_a_valid_one(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, child_prompts: list[str]
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    fields = ("delivery_fee", "drink_plan", "staffing_setup")
    schema = {"type": "object", "properties": {key: {"type": "string"} for key in fields}, "required": list(fields)}
    leaked = 'x</delivery_fee>\n<parameter name="drink_plan">y</drink_plan>\n<parameter name="staffing_setup">z'
    valid = {"delivery_fee": "x", "drink_plan": "y", "staffing_setup": "z</staffing_setup>"}
    model = _ScriptedModel(
        [
            [scripted_call("finish", {"delivery_fee": leaked}, call_id="leaked")],
            [scripted_call("finish", {"delivery_fee": "x", "drink_plan>": "y", "staffing_setup": "z"}, call_id="key")],
            [
                scripted_call("finish", valid, call_id="f1"),
                scripted_call("run_block", {"block_type": "text_prompt", "arguments": {"prompt": "Hi"}}, call_id="t"),
                scripted_call("finish", {**valid, "drink_plan": "changed"}, call_id="f2"),
            ],
        ]
    )

    result, _ = await _run_agent_block(monkeypatch, LUNA_MEDIUM, model, data_schema=schema)

    (leaked_reply,) = tool_outputs_for(model.inputs[1][1], "leaked")
    assert all(key in leaked_reply for key in fields)
    (key_reply,) = tool_outputs_for(model.inputs[2][1], "key")
    assert "drink_plan>" in key_reply
    rows = await database.observer.get_workflow_run_blocks(
        workflow_run_id=RUN, organization_id=ORG, with_tool_calls=True
    )
    assert [row.block_type for row in rows] == [BlockType.AGENT_STEP]
    assert child_prompts == []
    assert result.status == BlockStatus.completed
    assert isinstance(result.output_parameter_value, dict)
    assert result.output_parameter_value["output"] == valid
    assert [(call["tool"], call["status"]) for call in result.output_parameter_value["tool_calls"]] == [
        ("finish", "invalid_arguments"),
        ("finish", "invalid_arguments"),
        ("finish", "completed"),
    ]
    assert [(call.tool, call.status) for call in rows[0].tool_calls or []] == [
        ("finish", "invalid_arguments"),
        ("finish", "invalid_arguments"),
        ("finish", "completed"),
        ("run_block", "skipped"),
        ("finish", "skipped"),
    ]


def _bare_state(workflow_run_block_id: str = "wrb_agent") -> agent_step_loop._RunState:
    return agent_step_loop._RunState(
        label="agent",
        workflow_id="w_1",
        workflow_run_id=RUN,
        workflow_run_block_id=workflow_run_block_id,
        organization_id=ORG,
        browser_session_id=None,
        run_context=app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts[RUN],
        credential_keys=[],
        ignore_workflow_system_prompt=False,
    )


@pytest.mark.asyncio
async def test_tool_calls_from_one_response_run_one_at_a_time_in_the_order_the_model_wrote_them(
    database: AgentDB,
) -> None:
    state = _bare_state()
    state.call_order = ["goto", "extract"]
    ran: list[str] = []

    async def call(call_id: str) -> str:
        async def invoke(arguments: str) -> str:
            ran.append(f"{call_id} start")
            await asyncio.sleep(0)
            ran.append(f"{call_id} end")
            return call_id

        context = ToolContext(None, tool_name="page", tool_call_id=call_id, tool_arguments="{}")
        return await agent_step_loop._in_turn(state, invoke)(context, "{}")

    # The SDK starts each call as its own task, and the later call can reach its tool first.
    extract = asyncio.create_task(call("extract"))
    await asyncio.sleep(0)
    goto = asyncio.create_task(call("goto"))

    assert await asyncio.gather(goto, extract) == ["goto", "extract"]
    assert ran == ["goto start", "goto end", "extract start", "extract end"]
    assert state.call_order == []


@pytest.mark.asyncio
async def test_arguments_too_deep_to_parse_are_still_skipped_after_finish(database: AgentDB) -> None:
    state = _bare_state()
    state.answer = {"answer": "done"}
    invoke = AsyncMock()
    nested = "[" * 100_000 + "]" * 100_000
    context = ToolContext(None, tool_name="page_extract", tool_call_id="late", tool_arguments=nested)

    await agent_step_loop._in_turn(state, invoke)(context, nested)
    assert state.trail_writer is not None
    await state.trail_writer

    invoke.assert_not_awaited()
    assert [(call.tool, call.status) for call in state.trail] == [("page_extract", "skipped")]
    assert state.trail[0].arguments.startswith('"[[[')


@pytest.mark.asyncio
async def test_unparseable_arguments_are_redacted_before_they_are_encoded_for_the_trail(database: AgentDB) -> None:
    state = _bare_state()
    state.run_context.secrets["quoted_secret"] = 'pa"ss-1234567'
    arguments = '{"text": "pa\\"ss-1234567'
    context = ToolContext(None, tool_name="page_fill", tool_call_id="fill", tool_arguments=arguments)

    await agent_step_loop._in_turn(state, AsyncMock(return_value="ok"))(context, arguments)
    assert state.trail_writer is not None
    await state.trail_writer

    shown = json.loads(state.trail[0].arguments)
    assert "ss-1234567" not in shown
    assert REDACTED_SECRET_PLACEHOLDER in shown


@pytest.mark.asyncio
async def test_a_call_still_runs_when_its_trail_entry_cannot_be_built(database: AgentDB) -> None:
    state = _bare_state()
    invoke = AsyncMock(return_value="ran")
    arguments = '{"block_type": "text_prompt"}'
    context = ToolContext(None, tool_name="run_block", tool_call_id="run", tool_arguments=arguments)

    def broken_describe(_: JsonValue) -> tuple[BlockType | None, JsonValue]:
        raise TypeError("describe failed")

    assert await agent_step_loop._in_turn(state, invoke, broken_describe)(context, arguments) == "ran"
    invoke.assert_awaited_once_with(arguments)
    assert [(call.tool, call.block_type, call.arguments) for call in state.trail] == [
        ("run_block", None, json.dumps(arguments))
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["finished_entry", "column_write"])
async def test_a_recorded_call_returns_its_result_when_its_trail_entry_or_write_fails(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, broken: str
) -> None:
    state = _bare_state()
    status = "completed"
    if broken == "finished_entry":
        status = "not_a_block_status"
    else:
        monkeypatch.setattr(
            database.observer, "update_workflow_run_block_tool_calls", AsyncMock(side_effect=RuntimeError("db down"))
        )

    async def invoke(arguments: str) -> str:
        agent_step_loop._record_call(state, {"url": "https://example.com"}, tool="page_goto", status=status)
        return "ran"

    context = ToolContext(None, tool_name="page_goto", tool_call_id="goto", tool_arguments="{}")

    assert await agent_step_loop._in_turn(state, invoke)(context, "{}") == "ran"
    assert state.trail_writer is not None
    await state.trail_writer
    assert [call["tool"] for call in state.calls] == ["page_goto"]
    assert [(call.index, call.tool, call.status) for call in state.trail] == [
        (1, "page_goto", None if broken == "finished_entry" else "completed")
    ]
    assert agent_step_loop._child_label(state, BlockType.TEXT_PROMPT) == "agent_text_prompt_2"


@pytest.mark.asyncio
async def test_a_non_ascii_trail_is_cut_to_fit_the_column_as_it_is_stored(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB
) -> None:
    monkeypatch.setattr(agent_step_loop, "_MAX_TRAIL_BYTES", 2_000)
    row = await database.observer.create_workflow_run_block(
        workflow_run_id=RUN, organization_id=ORG, block_type=BlockType.AGENT_STEP, label="agent"
    )
    state = _bare_state(row.workflow_run_block_id)
    text = "\u65e5\u672c" * 75
    state.trail = [
        AgentStepToolCall(
            index=index, tool="page_type", status="completed", arguments=text, started_at=datetime.now(UTC)
        )
        for index in (1, 2, 3)
    ]
    state.pending_trail = state.trail

    await agent_step_loop._write_trail(state)

    async with database.Session() as session:
        stored = await session.scalar(
            select(WorkflowRunBlockModel.tool_calls).where(
                WorkflowRunBlockModel.workflow_run_block_id == row.workflow_run_block_id
            )
        )
    assert len(json.dumps(stored)) <= 2_000
    assert [call["arguments"] for call in stored] == ["", "", text]


def _record_tool_call_writes(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, gate: asyncio.Event | None = None
) -> list[list[dict[str, Any]]]:
    written: list[list[dict[str, Any]]] = []
    update = database.observer.update_workflow_run_block_tool_calls

    async def recording_update(
        workflow_run_block_id: str, organization_id: str | None, tool_calls: list[AgentStepToolCall]
    ) -> None:
        if gate is not None:
            await gate.wait()
        written.append([call.model_dump(mode="json", exclude_none=True) for call in tool_calls])
        await update(workflow_run_block_id, organization_id, tool_calls)

    monkeypatch.setattr(database.observer, "update_workflow_run_block_tool_calls", recording_update)
    return written


@pytest.mark.asyncio
async def test_each_call_is_written_to_tool_calls_while_it_runs_and_after_it_finishes_in_run_order(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, page: ScriptSkyvernPage, billing_hook: AsyncMock
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts[RUN].parameters["login_credential"] = _credential_parameter()
    written = _record_tool_call_writes(monkeypatch, database)

    async def sign_in(block: LoginBlock, workflow_run_id: str, workflow_run_block_id: str, **_: object) -> BlockResult:
        return BlockResult(
            success=True,
            output_parameter=block.output_parameter,
            output_parameter_value={"signed_in": True},
            status=BlockStatus.completed,
            workflow_run_block_id=workflow_run_block_id,
        )

    monkeypatch.setattr(LoginBlock, "execute", sign_in)
    model = _ScriptedModel(
        [
            [ResponseFunctionToolCall(id="fc_bad", call_id="bad", name="login", arguments="{no", type="function_call")],
            [scripted_call("login", {"credential": "login_credential", "url": "https://example.com/"}, "login")],
            [scripted_call("run_block", {"block_type": "text_prompt", "arguments": {"prompt": f"Say {SECRET}"}}, "t")],
            [scripted_call("run_block", {"block_type": "text_prompt", "arguments": {"bogus": 1}}, "unconverted")],
            [
                scripted_call("page_extract", {"prompt": "price"}, call_id="extract"),
                scripted_call("read_run_data", {}, call_id="index"),
            ],
            [scripted_call("finish", {"answer": "42"}, call_id="finish")],
        ]
    )

    with skyvern_context.scoped(SkyvernContext(workflow_run_id=RUN, organization_id=ORG)):
        result, _ = await _run_agent_block(
            monkeypatch,
            LUNA_MEDIUM,
            model,
            tools=["login", "text_prompt", "page_extract"],
            parameter_keys=["login_credential"],
        )

    assert result.status == BlockStatus.completed
    rows = await database.observer.get_workflow_run_blocks(
        workflow_run_id=RUN, organization_id=ORG, with_tool_calls=True
    )
    (agent_row,) = (row for row in rows if row.block_type == BlockType.AGENT_STEP)
    children = {row.label: row.workflow_run_block_id for row in rows if row is not agent_row}
    trail = agent_row.tool_calls
    assert trail is not None
    assert [(call.index, call.tool, call.status, call.label) for call in trail] == [
        (1, "login", "invalid_arguments", None),
        (2, "login", "completed", "agent_login_2"),
        (3, "run_block", "completed", "agent_text_prompt_3"),
        (4, "run_block", "invalid_arguments", "agent_text_prompt_4"),
        (5, "page_extract", "completed", None),
        (6, "read_run_data", "completed", None),
        (7, "finish", "completed", None),
    ]
    labels = [call.label for call in trail if call.label]
    assert len(labels) == len(set(labels))
    assert [call.child_workflow_run_block_id for call in trail[:4]] == [
        None,
        children["agent_login_2"],
        children["agent_text_prompt_3"],
        None,
    ]
    login_block, prompt_block = (json.loads(call.block or "null") for call in trail[1:3])
    assert (login_block["block_type"], login_block["parameter_keys"]) == ("login", ["login_credential"])
    assert "credential" not in login_block
    assert prompt_block["label"] == "agent_text_prompt_3"
    assert [call.block for call in [trail[0], *trail[3:]]] == [None] * 5
    assert all(call.finished_at is not None and call.started_at <= call.finished_at for call in trail)
    assert SECRET not in json.dumps(written)

    output = result.output_parameter_value
    assert isinstance(output, dict)
    assert [(call.tool, call.status, call.arguments) for call in trail[1:]] == [
        (call["tool"], call["status"], call["arguments"]) for call in output["tool_calls"]
    ]
    assert not any(
        {"index", "started_at", "finished_at", "block", "child_workflow_run_block_id"} & call.keys()
        for call in output["tool_calls"]
    )
    assert _billed_decisions(billing_hook) == 5

    assert [(call["index"], call["status"]) for call in written[0]] == [(1, "invalid_arguments")]
    # The writer keeps only the newest pending snapshot, so which running snapshots land depends on scheduling.
    assert all([call["index"] for call in snapshot] == list(range(1, len(snapshot) + 1)) for snapshot in written)
    lengths = [len(snapshot) for snapshot in written]
    assert lengths == sorted(lengths)
    final = {call.index: call.label for call in trail}
    for snapshot in written:
        if snapshot and snapshot[-1]["status"] == "running":
            assert snapshot[-1].get("label") == final[snapshot[-1]["index"]]
    assert written[-1] == [call.model_dump(mode="json", exclude_none=True) for call in trail]


@pytest.mark.asyncio
@pytest.mark.parametrize("compact", [False, True])
async def test_tools_do_not_wait_for_tool_calls_writes_and_long_calls_are_cut_to_fit(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, compact: bool
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    if compact:
        monkeypatch.setattr(agent_step_loop, "_MAX_TRAIL_BYTES", 2_000)
    gate = asyncio.Event()
    written = _record_tool_call_writes(monkeypatch, database, gate)
    prompt = "x" * 9_000

    def finish_after_the_prompt_ran(model_input: list[Any]) -> list[TResponseOutputItem]:
        assert json.loads(tool_outputs_for(model_input, "long")[0])["status"] == "completed"
        assert written == []
        gate.set()
        return VALID_FINISH

    model = _ScriptedModel(
        [
            [scripted_call("run_block", {"block_type": "text_prompt", "arguments": {"prompt": prompt}}, "long")],
            [scripted_call("run_block", {"block_type": "text_prompt", "arguments": {"prompt": "Say hi"}}, "short")],
            finish_after_the_prompt_ran,
        ]
    )

    result, _ = await _run_agent_block(monkeypatch, LUNA_MEDIUM, model, tools=["text_prompt"])

    assert result.status == BlockStatus.completed
    rows = await database.observer.get_workflow_run_blocks(
        workflow_run_id=RUN, organization_id=ORG, with_tool_calls=True
    )
    (agent_row,) = (row for row in rows if row.block_type == BlockType.AGENT_STEP)
    assert agent_row.tool_calls is not None
    assert written[-1] == [call.model_dump(mode="json", exclude_none=True) for call in agent_row.tool_calls]
    assert all(call.status != "running" for call in agent_row.tool_calls)
    first, newest = agent_row.tool_calls[:2]
    assert (first.index, first.tool, first.label) == (1, "run_block", "agent_text_prompt_1")
    assert json.loads(newest.block or "null")["label"] == "agent_text_prompt_2"
    assert "Say hi" in newest.arguments
    if compact:
        assert (first.arguments, first.block) == ("", None)
    else:
        assert prompt in (first.block or "")
        assert first.arguments.endswith("...[truncated]")
        assert len(first.arguments) == agent_step_loop._MAX_TRAIL_ARGUMENT_CHARS + len("...[truncated]")
    assert isinstance(result.output_parameter_value, dict)
    assert prompt in result.output_parameter_value["tool_calls"][0]["arguments"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("column", "read_tools"),
    [
        ({"truncated": True, "reason": "exceeded_max_jsonb_value_size"}, None),
        (
            [
                {"index": 1, "tool": "page_goto", "started_at": "2026-11-06T00:00:00Z", "arguments": "{}"},
                {"index": "two", "tool": "page_click"},
            ],
            ["page_goto"],
        ),
    ],
)
async def test_a_tool_calls_column_reads_only_its_valid_calls(
    database: AgentDB, column: JsonValue, read_tools: list[str] | None
) -> None:
    row = await database.observer.create_workflow_run_block(
        workflow_run_id=RUN, organization_id=ORG, block_type=BlockType.AGENT_STEP, label="agent"
    )
    async with database.Session() as session:
        await session.execute(
            update(WorkflowRunBlockModel)
            .where(WorkflowRunBlockModel.workflow_run_block_id == row.workflow_run_block_id)
            .values(tool_calls=column)
        )
        await session.commit()

    (stored,) = await database.observer.get_workflow_run_blocks(
        workflow_run_id=RUN, organization_id=ORG, with_tool_calls=True
    )
    (unloaded,) = await database.observer.get_workflow_run_blocks(workflow_run_id=RUN, organization_id=ORG)

    assert (None if stored.tool_calls is None else [call.tool for call in stored.tool_calls]) == read_tools
    assert unloaded.tool_calls is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("cut_off", "status"), [("invalid_definition", "failed"), ("cancelled", "canceled")])
async def test_a_call_cut_off_mid_run_keeps_its_entry_and_the_final_tool_calls_has_no_running_call(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, billing_hook: AsyncMock, cut_off: str, status: str
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    block_task = asyncio.current_task()
    assert block_task is not None

    async def cut_off_read(state: agent_step_loop._RunState, name: str | None = None) -> object:
        if cut_off == "invalid_definition":
            raise InvalidWorkflowDefinition("bad definition")
        block_task.cancel()
        await asyncio.sleep(1)
        return None

    monkeypatch.setattr(agent_step_loop, "_read_run_data", cut_off_read)
    model = _ScriptedModel([[scripted_call("read_run_data", {"name": "x"}, call_id="read")], VALID_FINISH])

    if cut_off == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await _run_agent_block(monkeypatch, LUNA_MEDIUM, model)
        block_task.uncancel()
    else:
        result, _ = await _run_agent_block(monkeypatch, LUNA_MEDIUM, model)
        assert result.status == BlockStatus.failed

    (agent_row,) = await database.observer.get_workflow_run_blocks(
        workflow_run_id=RUN, organization_id=ORG, with_tool_calls=True
    )
    assert [(call.index, call.tool, call.status) for call in agent_row.tool_calls or []] == [
        (1, "read_run_data", status)
    ]
    output = app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts[RUN].values["agent_output"]
    assert output["tool_calls"] == []
    assert _billed_decisions(billing_hook) == 0


EVERYONE_NAME = "shared-menu-helper"
EVERYONE_DESCRIPTION = "Handle the example.com menu quirk."
EVERYONE_BODY = "Open the hidden menu before the form.\n"
EVERYONE_FILE = "The menu sits behind the gear icon."
EVERYONE_SKILL = available_skill(
    EVERYONE_NAME, "everyone", EVERYONE_BODY, EVERYONE_DESCRIPTION, **{"notes/menu.md": EVERYONE_FILE}
)
ORG_BODY = "Tick 'Remember this device' before submitting.\n"
ORG_SKILL = available_skill("example-portal-login", "organization", ORG_BODY, **{"reference/form.md": "Form notes"})
WORKFLOW_SKILL = available_skill("invoice-download", "workflow", "Download the newest invoice.\n")


def _skills_hook(monkeypatch: pytest.MonkeyPatch, skills: list[AvailableSkill] | Exception) -> RecordingSkills:
    hook = RecordingSkills(skills)
    monkeypatch.setattr(app.AGENT_FUNCTION, "agent_skills", hook.agent_skills)
    return hook


def _reports(model: _ScriptedModel, *call_ids: str) -> dict[str, Any]:
    return {call_id: json.loads(tool_outputs_for(model.inputs[-1][1], call_id)[0]) for call_id in call_ids}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tools", "skills", "ignore_workflow_system_prompt", "offered"),
    [
        (["page_goto", "load_skill"], [ORG_SKILL, EVERYONE_SKILL], False, True),
        (["page_goto", "load_skill"], [ORG_SKILL, EVERYONE_SKILL], True, True),
        (["page_goto"], [ORG_SKILL, EVERYONE_SKILL], False, False),
        (["page_goto", "load_skill"], [], False, False),
        (["page_goto", "load_skill"], RuntimeError("skill store down"), False, False),
    ],
)
async def test_a_step_gets_the_skill_list_and_skill_tools_only_when_it_lists_load_skill_and_the_run_has_skills(
    monkeypatch: pytest.MonkeyPatch,
    database: AgentDB,
    tools: list[str],
    skills: list[AvailableSkill] | Exception,
    ignore_workflow_system_prompt: bool,
    offered: bool,
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts[RUN].inherited_workflow_system_prompt = "Answer in French."
    hook = _skills_hook(monkeypatch, skills)
    model = _ScriptedModel([VALID_FINISH])

    with capture_logs() as logs:
        result, _ = await _run_agent_block(
            monkeypatch, LUNA_MEDIUM, model, tools=tools, ignore_workflow_system_prompt=ignore_workflow_system_prompt
        )

    assert result.status == BlockStatus.completed
    assert hook.calls == ([(ORG, WPID, "agent_step")] if "load_skill" in tools else [])
    warned = [log for log in logs if log["event"] == "Failed to list the agent step's skills"]
    assert len(warned) == isinstance(skills, Exception)
    system = str(model.inputs[0][0])
    descriptions = [ORG_SKILL.package.description, EVERYONE_DESCRIPTION]
    assert all((description in system) is offered for description in descriptions)
    if offered and not ignore_workflow_system_prompt:
        assert system.index("Answer in French.") < min(system.index(description) for description in descriptions)
    skill_tools = ["load_skill", "read_skill_resource"] if offered else []
    assert model.tool_names == [["read_run_data", "page_goto", *skill_tools, "finish"]]


def _largest_skill_with_secret() -> tuple[AvailableSkill, str]:
    files = {f"reference/{index:03d}.md": "é" for index in range(MAX_PACKAGE_FILES - 1)}
    body = ""
    low, high = 0, MAX_SKILL_RESULT_CHARS
    while low <= high:
        middle = (low + high) // 2
        candidate = "é" * 2_000 + SECRET + "a" * middle
        try:
            SkillPackage.from_files({"SKILL.md": skill_md("example-max-skill", candidate), **files})
            body, low = candidate, middle + 1
        except ValueError:
            high = middle - 1
    return available_skill("example-max-skill", "organization", body, **files), body


@pytest.mark.asyncio
async def test_skill_tools_return_uncut_masked_results_and_refusals_as_completed_billed_calls(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, billing_hook: AsyncMock
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    largest, body = _largest_skill_with_secret()
    _skills_hook(monkeypatch, [EVERYONE_SKILL, ORG_SKILL, WORKFLOW_SKILL, largest])
    name = largest.package.name
    loaded = SkillLoaded(name=name, version=largest.version, body=body, files=largest.package.resource_paths)
    text = skill_md(name, body)
    assert len(largest.package.resource_paths) == MAX_PACKAGE_FILES - 1
    sizes = [skill_result_chars(loaded), skill_result_chars(SkillResource(name=name, path="SKILL.md", text=text))]
    assert max(sizes) == MAX_SKILL_RESULT_CHARS
    org = "example-portal-login"
    model = _ScriptedModel(
        [
            [scripted_call("read_skill_resource", {"name": org, "path": "reference/form.md"}, call_id="r")],
            [scripted_call("load_skill", {"name": name}, call_id="load")],
            [scripted_call("read_skill_resource", {"name": name, "path": "SKILL.md"}, call_id="read")],
            [scripted_call("load_skill", {"name": org}, call_id="first")],
            [scripted_call("load_skill", {"name": org}, call_id="again")],
            [scripted_call("load_skill", {"name": "nope"}, call_id="unknown")],
            [scripted_call("read_skill_resource", {"name": "nope", "path": "SKILL.md"}, call_id="unknown_read")],
            [scripted_call("read_skill_resource", {"name": org, "path": "nope.md"}, call_id="no_file")],
            VALID_FINISH,
        ]
    )

    with capture_logs() as logs:
        await _run_agent_block(monkeypatch, LUNA_MEDIUM, model, tools=["load_skill"])

    reports = _reports(model, "r", "load", "read", "first", "again", "unknown", "unknown_read", "no_file")
    assert SECRET not in json.dumps(reports)
    assert all((report["status"], report["error"]) == ("completed", None) for report in reports.values())
    assert reports["r"]["output"] == {"name": org, "path": "reference/form.md", "text": "Form notes"}
    assert reports["load"]["output"] == {**loaded, "body": body.replace(SECRET, "*****")}
    assert reports["read"]["output"]["text"] == text.replace(SECRET, "*****")
    assert reports["first"]["output"]["body"] == ORG_BODY
    assert reports["again"]["output"] == {"status": "already_loaded", "name": org, "version": ORG_SKILL.version}
    unknown = {
        "error": "unknown_skill",
        "message": "No skill is named 'nope'.",
        "names": [name, org, "invoice-download"],
    }
    assert reports["unknown"]["output"] == reports["unknown_read"]["output"] == unknown
    assert reports["no_file"]["output"] == {
        "error": "unknown_file",
        "message": f"{org} has no file 'nope.md'.",
        "files": ["SKILL.md", "reference/form.md"],
    }
    assert _billed_decisions(billing_hook) == 8
    rows = await database.observer.get_workflow_run_blocks(workflow_run_id=RUN, organization_id=ORG)
    (agent_row,) = (row for row in rows if row.block_type == BlockType.AGENT_STEP)
    events = [log for log in logs if log["event"].startswith("agent_skill_")]
    assert [log["event"] for log in events].count("agent_skill_list_shown") == 1
    assert {log["event"] for log in events} == {
        "agent_skill_list_shown",
        "agent_skill_loaded",
        "agent_skill_resource_read",
    }
    assert all(
        (log["agent"], log["organization_id"], log["workflow_run_id"], log["workflow_run_block_id"])
        == ("agent_step", ORG, RUN, agent_row.workflow_run_block_id)
        for log in events
    )


@pytest.mark.asyncio
async def test_everyone_skill_calls_leave_no_skill_name_or_text_in_the_trail_tool_calls_or_stored_model_calls(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, tmp_path: Path
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    monkeypatch.setattr(app, "ARTIFACT_MANAGER", ArtifactManager())
    monkeypatch.setattr(app, "STORAGE", LocalStorage(str(tmp_path)))
    _skills_hook(monkeypatch, [EVERYONE_SKILL, ORG_SKILL])
    running: dict[int, AgentStepToolCall] = {}
    publish = agent_step_loop._publish_trail

    def recording_publish(state: agent_step_loop._RunState) -> asyncio.Task[None]:
        if state.running is not None:
            running[state.running.index] = state.running
        return publish(state)

    monkeypatch.setattr(agent_step_loop, "_publish_trail", recording_publish)
    unparseable = ResponseFunctionToolCall(
        id="fc_bad", call_id="bad", name="load_skill", arguments=f'{{"name": "{EVERYONE_NAME}"', type="function_call"
    )
    model = _ScriptedModel(
        [
            [scripted_call("load_skill", {"name": EVERYONE_NAME}, call_id="e1")],
            [scripted_call("read_skill_resource", {"name": EVERYONE_NAME, "path": "notes/menu.md"}, call_id="e2")],
            [scripted_call("read_skill_resource", {"name": "example-portal-login", "path": "reference/form.md"}, "o")],
            [scripted_call("load_skill", {"name": "example-portal-login"}, call_id="o1")],
            [scripted_call("load_skill", {"name": "missing-skill"}, call_id="u")],
            [unparseable],
            VALID_FINISH,
        ]
    )

    result, _ = await _run_agent_block(monkeypatch, LUNA_MEDIUM, model, tools=["load_skill"])

    hidden = {"name": EVERYONE_SKILL_LABEL}
    org_read = {"name": "example-portal-login", "path": "reference/form.md"}
    org_load = {"name": "example-portal-login"}
    assert [(call.tool, json.loads(call.arguments)) for call in running.values()] == [
        ("load_skill", hidden),
        ("read_skill_resource", hidden),
        ("read_skill_resource", org_read),
        ("load_skill", org_load),
        ("load_skill", hidden),
        ("load_skill", hidden),
        ("finish", {"answer": "42"}),
    ]
    rows = await database.observer.get_workflow_run_blocks(
        workflow_run_id=RUN, organization_id=ORG, with_tool_calls=True
    )
    (agent_row,) = (row for row in rows if row.block_type == BlockType.AGENT_STEP)
    assert agent_row.tool_calls is not None
    assert [(call.tool, call.status, json.loads(call.arguments)) for call in agent_row.tool_calls] == [
        ("load_skill", "completed", hidden),
        ("read_skill_resource", "completed", hidden),
        ("read_skill_resource", "completed", org_read),
        ("load_skill", "completed", org_load),
        ("load_skill", "completed", hidden),
        ("load_skill", "invalid_arguments", hidden),
        ("finish", "completed", {"answer": "42"}),
    ]
    output = result.output_parameter_value
    assert isinstance(output, dict)
    assert [(call["tool"], json.loads(call["arguments"])) for call in output["tool_calls"]] == [
        ("load_skill", hidden),
        ("read_skill_resource", hidden),
        ("read_skill_resource", org_read),
        ("load_skill", org_load),
        ("load_skill", hidden),
        ("finish", {"answer": "42"}),
    ]
    hidden_text = [EVERYONE_NAME, EVERYONE_DESCRIPTION, EVERYONE_BODY.strip(), EVERYONE_FILE, "missing-skill"]
    received = json.dumps(pydantic_core.to_jsonable_python(model.inputs))
    assert all(text in received for text in hidden_text)
    artifacts = await database.artifacts.get_artifacts_by_entity_id(
        organization_id=ORG,
        workflow_run_block_id=agent_row.workflow_run_block_id,
        artifact_types=[ArtifactType.LLM_REQUEST, ArtifactType.LLM_RESPONSE],
    )
    stored: list[str] = []
    for artifact in artifacts:
        data = await app.ARTIFACT_MANAGER.retrieve_artifact(artifact)
        assert data is not None
        stored.append(data.decode())
    assert len(stored) == 2 * len(model.inputs)
    assert not [text for text in hidden_text for body in stored if text in body]
    requests = [body for body in map(json.loads, stored) if "system_prompt" in body]
    assert len(requests) == len(model.inputs)
    assert all(f"{EVERYONE_SKILL_LABEL} [everyone]" in request["system_prompt"] for request in requests)
    assert any(ORG_BODY.strip() in body for body in stored)
