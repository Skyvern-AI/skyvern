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
from litellm.types.utils import ModelResponse as LitellmResponse
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from skyvern.core.script_generations.real_skyvern_page_ai import _render_template_with_label
from skyvern.core.script_generations.script_skyvern_page import ScriptSkyvernPage
from skyvern.forge import app
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.forge.sdk.api.llm.copilot_model_usage import CopilotModelUsageEvent, _observe_copilot_model_usage
from skyvern.forge.sdk.artifact.manager import ArtifactManager
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.artifact.storage.local import LocalStorage
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import WorkflowRunBlockModel, WorkflowRunModel
from skyvern.forge.sdk.experimentation.providers import NoOpExperimentationProvider
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager, WorkflowRunContext
from skyvern.forge.sdk.workflow.models import agent_step_loop, agent_step_tools
from skyvern.forge.sdk.workflow.models.agent_step_block import AgentStepBlock
from skyvern.forge.sdk.workflow.models.parameter import (
    PLATFORM_SMTP_AWS_KEYS,
    AWSSecretParameter,
    WorkflowParameter,
    WorkflowParameterType,
)
from skyvern.forge.sdk.workflow.workflow_definition_converter import block_yaml_to_block
from skyvern.schemas.llm import LLMConfig
from skyvern.schemas.workflows import AgentStepBlockYAML, BlockResult, BlockStatus, BlockType
from tests.unit.conftest import make_block_output_parameter
from tests.unit.copilot_test_helpers import scripted_call, scripted_text, tool_outputs_for
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
        _observe_copilot_model_usage(CopilotModelUsageEvent(request_model="scripted", cost=MODEL_CALL_COST))
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
        _observe_copilot_model_usage(CopilotModelUsageEvent(request_model="child", cost=100.0))
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
    run_context.secrets["api_token"] = SECRET
    run_context.values["api_token"] = SECRET
    app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts[RUN] = run_context
    return database


@pytest.fixture
def billing_hook(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    hook = AsyncMock()
    monkeypatch.setattr(app.AGENT_FUNCTION, "post_agent_step_execution", hook)
    return hook


@pytest.fixture
def page(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    page = SimpleNamespace(
        page=SimpleNamespace(url="https://example.com/"), extract=AsyncMock(return_value="extracted")
    )

    async def create(browser_session_id: str | None = None) -> SimpleNamespace:
        return page

    monkeypatch.setattr(ScriptSkyvernPage, "create", create)
    return page


def _billed_decisions(hook: AsyncMock) -> int:
    hook.assert_awaited_once()
    assert hook.await_args is not None
    assert hook.await_args.args[:2] == (ORG, RUN)
    return hook.await_args.kwargs["decisions"]


def _litellm_replies(monkeypatch: pytest.MonkeyPatch, *replies: dict[str, Any]) -> list[dict[str, Any]]:
    """Answer each litellm.acompletion call with the next choice and return the requests sent."""
    sent: list[dict[str, Any]] = []

    async def fake_acompletion(**kwargs: object) -> LitellmResponse:
        sent.append(kwargs)
        return LitellmResponse(choices=[{"index": 0, **replies[len(sent) - 1]}])

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    return sent


def _litellm_call(name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
    function = {"name": name, "arguments": json.dumps(arguments)}
    return {
        "finish_reason": "tool_calls",
        "message": {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "function": function}]},
    }


async def _run_agent_block(
    monkeypatch: pytest.MonkeyPatch,
    variant: str | None,
    model: _ScriptedModel | None,
    max_turns: int = 25,
    prompt: str = "Find the answer.",
    tools: list[str] | None = None,
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
        AgentStepBlockYAML(label="agent", prompt=prompt, max_turns=max_turns, tools=tools, **block_fields),
        {"agent_output": make_block_output_parameter("agent_output")},
    )
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
    # This run has no credential parameter, so the step is not given login.
    assert set(given) | set(agent_step_tools.RUN_BLOCK_TYPES) | {"login"} == set(shown)
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tools", "credential", "offered", "block_types"),
    [
        (["http_request", "web_search"], False, ["web_search", "run_block", "finish"], ["http_request"]),
        (None, False, ALL_TOOLS, list(agent_step_tools.RUN_BLOCK_TYPES)),
        (["web_search"], False, ["web_search", "finish"], None),
        (["login", "page_goto"], False, ["page_goto", "finish"], None),
        (["login", "page_goto"], True, ["login", "page_goto", "finish"], None),
        (["finish"], False, ["finish"], None),
    ],
)
async def test_a_step_is_offered_only_the_tools_it_lists_and_finish(
    monkeypatch: pytest.MonkeyPatch,
    database: AgentDB,
    tools: list[str] | None,
    credential: bool,
    offered: list[str],
    block_types: list[str] | None,
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    if credential:
        app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts[RUN].parameters["login_credential"] = _credential_parameter()
    model = _ScriptedModel([VALID_FINISH])

    await _run_agent_block(monkeypatch, LUNA_MEDIUM, model, tools=tools)

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
async def test_a_run_block_call_for_an_unlisted_block_type_runs_no_child(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, child_prompts: list[str]
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    model = _ScriptedModel(
        [
            [scripted_call("run_block", {"block_type": "text_prompt", "arguments": {"prompt": "Hi"}}, call_id="t")],
            [scripted_call("finish", {"answer": "done"}, call_id="f")],
        ]
    )

    await _run_agent_block(monkeypatch, LUNA_MEDIUM, model, tools=["http_request"])

    rows = await database.observer.get_workflow_run_blocks(workflow_run_id=RUN, organization_id=ORG)
    assert [row.block_type for row in rows] == [BlockType.AGENT_STEP]
    assert child_prompts == []
    assert "['http_request']" in tool_outputs_for(model.inputs[1][1], "t")[0]


@pytest.mark.parametrize("name", ["run_block", "update_workflow", "create_schedule", "not_a_tool"])
def test_a_tool_the_step_cannot_be_given_fails_at_save_and_the_error_lists_the_valid_tools(name: str) -> None:
    with pytest.raises(ValidationError) as error:
        AgentStepBlockYAML(label="agent", prompt="Find the answer.", tools=["http_request", name])

    message = str(error.value)
    assert f"Unknown tools [{name!r}]" in message
    assert ", ".join(sorted(agent_step_tools.tool_descriptions())) in message


def test_an_empty_tools_list_fails_at_save_because_no_list_means_every_tool() -> None:
    with pytest.raises(ValidationError, match="at least 1 item"):
        AgentStepBlockYAML(label="agent", prompt="Find the answer.", tools=[])

    assert AgentStepBlockYAML(label="agent", prompt="Find the answer.").tools is None


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
async def test_the_agent_block_sends_its_pinned_effort_on_a_bare_gpt_5_name(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _litellm_replies(monkeypatch, {"message": {"role": "assistant", "content": "ok"}})
    monkeypatch.setitem(LLMConfigRegistry._configs, "GPT5_PINNED_TEST", _pinned("high", "gpt-5.4"))
    model, run_config = agent_step_loop.resolve_agent_model("GPT5_PINNED_TEST")

    await Runner.run(Agent(name="agent-step", model=model), "hello", run_config=run_config)

    (request,) = sent
    assert request["reasoning_effort"] == "high"


def test_every_block_tool_schema_defines_exactly_the_definitions_it_references() -> None:
    state = cast(agent_step_loop._RunState, SimpleNamespace(credential_keys=["login_credential"]))
    for tool in agent_step_tools.AGENT_BLOCK_TOOLS.values():
        if isinstance(tool, agent_step_tools.BlockTool):
            schema = agent_step_loop._arguments_schema(tool, state)
            references = set(re.findall(r'"#/\$defs/(\w+)"', json.dumps(schema)))
            assert references == set(schema.get("$defs", {})), tool.yaml_class.__name__


def test_every_block_type_is_an_agent_tool_or_says_why_not() -> None:
    assert set(agent_step_tools.AGENT_BLOCK_TOOLS) == set(BlockType)
    for block_type, tool in agent_step_tools.AGENT_BLOCK_TOOLS.items():
        if isinstance(tool, str):
            assert tool
            continue
        assert tool.yaml_class.model_fields["block_type"].default == block_type
        assert set(tool.fields) - {"credential"} <= set(tool.yaml_class.model_fields)


EMAIL = {"sender": "agent@example.com", "subject": "Report", "body": "Done."}


@pytest.mark.asyncio
@pytest.mark.usefixtures("offline_address_validation")
async def test_a_send_email_child_sends_through_the_platform_smtp_secrets(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
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
            [scripted_call("finish", {"answer": "sent"}, call_id="c2")],
        ]
    )

    with patch("smtplib.SMTP") as smtp:
        await _run_agent_block(monkeypatch, LUNA_MEDIUM, model, prompt="Email the report to the address on the page.")

    rows = await database.observer.get_workflow_run_blocks(workflow_run_id=RUN, organization_id=ORG)
    children = [(row.block_type, row.status) for row in rows if row.parent_workflow_run_block_id]
    assert children == [(BlockType.SEND_EMAIL, "completed")]
    assert smtp.call_args.args[0] == "smtp.example.com"
    assert "smtp-password" not in json.dumps(model.inputs, default=str)


@pytest.mark.asyncio
@pytest.mark.parametrize("ignore", [False, True])
async def test_the_agent_and_what_it_runs_follow_the_workflow_system_prompt_unless_the_block_opts_out(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, page: SimpleNamespace, ignore: bool
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
    # A secret parameter's value is the id its secret is registered under, not the secret.
    run_context.values["api_token"] = "secret_id_api_token"
    run_context.secrets["secret_id_api_token"] = SECRET
    await run_context.register_output_parameter_value_post_execution(
        make_block_output_parameter("search_output"), {"extracted_information": {"price": 42, "token": SECRET}}
    )
    run_context.record_block_outcome("search", BlockStatus.completed, [], None)

    async def downloaded_files(organization_id: str, run_id: str | None) -> list[FileInfo]:
        return [FileInfo(url="https://example.com/invoice.pdf", filename="invoice.pdf")]

    monkeypatch.setattr(app, "STORAGE", SimpleNamespace(get_downloaded_files=downloaded_files))
    model = _ScriptedModel(
        [
            [scripted_call("read_run_data", {}, call_id="index")],
            [scripted_call("read_run_data", {"name": "search"}, call_id="search")],
            [scripted_call("read_run_data", {"name": "api_token"}, call_id="secret")],
            [scripted_call("finish", {"answer": "42"}, call_id="finish")],
        ]
    )

    result, _ = await _run_agent_block(monkeypatch, LUNA_MEDIUM, model)

    assert result.status == BlockStatus.completed
    assert _tool_output(model, "index") == {
        "parameters": ["api_token"],
        "blocks": {"search": "completed"},
        "downloaded_files": ["invoice.pdf"],
    }
    search = _tool_output(model, "search")
    assert search["output"]["extracted_information"] == {"price": 42, "token": "*****"}
    assert search["outcome"]["status"] == "completed"
    assert _tool_output(model, "secret") == "*****"
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
async def test_a_step_without_read_run_data_sees_only_the_size_of_a_cut_child_result(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    report_text = _big_child_output(monkeypatch)
    model = _ScriptedModel([TEXT_PROMPT_CHILD, VALID_FINISH])

    await _run_agent_block(monkeypatch, LUNA_MEDIUM, model, tools=["text_prompt"])

    (first,) = tool_outputs_for(model.inputs[1][1], "child")
    assert first.startswith(report_text[:20_000])
    assert str(_CHILD_RESULT_CHARS) in first[20_000:]
    assert "read_run_data(" not in first


@pytest.mark.asyncio
async def test_bad_read_run_data_reads_fail_within_the_cap_and_a_cut_page_result_gives_only_its_size(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    _big_child_output(monkeypatch)

    class Page:
        page = SimpleNamespace(url="https://example.com/")

        async def extract(self, prompt: str, schema: dict[str, Any] | None = None) -> str:
            return "p" * 30_000

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

    model = _ScriptedModel(
        [
            TEXT_PROMPT_CHILD,
            bad_reads,
            [scripted_call("page_extract", {"prompt": "everything"}, call_id="page")],
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
    (page,) = tool_outputs_for(model.inputs[3][1], "page")
    page_total = len(json.dumps({"status": "completed", "output": "p" * 30_000, "error": None}))
    assert str(page_total) in page[20_000:]
    assert "read_run_data(" not in page


@pytest.mark.asyncio
async def test_page_commands_run_under_one_agent_task_and_reach_page_ai_as_written(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, page: SimpleNamespace
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    bound: list[tuple[str | None, str | None]] = []

    async def extract(prompt: str, schema: dict[str, Any] | None = None) -> str:
        context = skyvern_context.ensure_context()
        bound.append((context.task_id, context.step_id))
        return _render_template_with_label(prompt)

    page.extract.side_effect = extract
    model = _ScriptedModel(
        [
            [scripted_call("page_extract", {"prompt": "Echo {{ api_token }}"}, call_id="template")],
            [scripted_call("page_extract", {"prompt": "api_token"}, call_id="key")],
            [scripted_call("finish", {"answer": "done"}, call_id="finish")],
        ]
    )

    with skyvern_context.scoped(SkyvernContext(workflow_run_id=RUN, organization_id=ORG)) as context:
        result, _ = await _run_agent_block(monkeypatch, LUNA_MEDIUM, model)

    assert result.status == BlockStatus.completed
    assert [_tool_output(model, call_id) for call_id in ("template", "key")] == ["Echo {{ api_token }}", "api_token"]
    (agent_row,) = await database.observer.get_workflow_run_blocks(workflow_run_id=RUN, organization_id=ORG)
    assert agent_row.task_id
    task = await database.tasks.get_task(agent_row.task_id, organization_id=ORG)
    (step,) = await database.tasks.get_task_steps(agent_row.task_id, organization_id=ORG)
    assert task is not None and (task.status, step.status) == (TaskStatus.completed, StepStatus.completed)
    assert bound == [(task.task_id, step.step_id)] * 2
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
async def test_no_call_runs_after_a_valid_finish_in_the_same_response(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, child_prompts: list[str]
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    model = _ScriptedModel(
        [
            [
                scripted_call("finish", {"answer": "done"}, call_id="f1"),
                scripted_call("run_block", {"block_type": "text_prompt", "arguments": {"prompt": "Hi"}}, call_id="t"),
                scripted_call("finish", {"answer": "changed"}, call_id="f2"),
            ]
        ]
    )

    result, _ = await _run_agent_block(monkeypatch, LUNA_MEDIUM, model)

    rows = await database.observer.get_workflow_run_blocks(workflow_run_id=RUN, organization_id=ORG)
    assert [row.block_type for row in rows] == [BlockType.AGENT_STEP]
    assert child_prompts == []
    assert result.status == BlockStatus.completed
    assert isinstance(result.output_parameter_value, dict)
    assert result.output_parameter_value["output"] == {"answer": "done"}


@pytest.mark.asyncio
async def test_tool_calls_from_one_response_run_one_at_a_time_in_the_order_the_model_wrote_them(
    database: AgentDB,
) -> None:
    state = agent_step_loop._RunState(
        label="agent",
        workflow_id="w_1",
        workflow_run_id=RUN,
        workflow_run_block_id="wrb_agent",
        organization_id=ORG,
        browser_session_id=None,
        run_context=app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts[RUN],
        credential_keys=[],
        ignore_workflow_system_prompt=False,
    )
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
