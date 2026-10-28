from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, NoReturn, cast
from unittest.mock import AsyncMock, MagicMock, patch

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
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from skyvern.core.script_generations.real_skyvern_page_ai import _render_template_with_label
from skyvern.core.script_generations.script_skyvern_page import ScriptSkyvernPage
from skyvern.forge import app
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.forge.sdk.api.llm.copilot_model_usage import CopilotModelUsageEvent, _observe_copilot_model_usage
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import WorkflowRunBlockModel, WorkflowRunModel
from skyvern.forge.sdk.experimentation.providers import NoOpExperimentationProvider
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager, WorkflowRunContext
from skyvern.forge.sdk.workflow.models import agent_step_loop
from skyvern.forge.sdk.workflow.models.parameter import PLATFORM_SMTP_AWS_KEYS, AWSSecretParameter
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


class _ScriptedModel(Model):
    def __init__(self, script: list[list[TResponseOutputItem]]) -> None:
        self.script = script
        self.inputs: list[list[str | list[TResponseInputItem] | None]] = []
        self.tool_names: list[list[str]] = []
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
        self.tool_descriptions.append({tool.name: tool.description for tool in tools if isinstance(tool, FunctionTool)})
        _observe_copilot_model_usage(CopilotModelUsageEvent(request_model="scripted", cost=MODEL_CALL_COST))
        return ModelResponse(output=self.script[len(self.inputs) - 1], usage=Usage(requests=1), response_id=None)

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
        AgentStepBlockYAML(label="agent", prompt=prompt, max_turns=max_turns, **block_fields),
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


@pytest.mark.asyncio
async def test_authors_are_shown_exactly_the_tools_the_step_gets(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    model = _ScriptedModel([VALID_FINISH])

    await _run_agent_block(monkeypatch, LUNA_MEDIUM, model)

    # This run has no credential parameter, so the step is not given login.
    shown = {name: text for name, text in agent_step_loop.tool_descriptions().items() if name != "login"}
    assert model.tool_descriptions == [shown]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "I can"])
async def test_a_model_refusal_fails_the_agent_block_without_asking_again(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, text: str
) -> None:
    # LiteLLM reports Anthropic's refusal stop reason as content_filter, with or without the text written before it.
    monkeypatch.setitem(LLMConfigRegistry._configs, HAIKU_MEDIUM, _pinned("medium", HAIKU))
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
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, ending: str
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))

    class RunEndingModel(_ScriptedModel):
        async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
            response = await super().get_response(*args, **kwargs)
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

    assert len(model.inputs) == 1
    output = app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts[RUN].values["agent_output"]
    assert (output["stop_reason"], output["tool_calls"]) == ("run_ended", [])
    (agent_row,) = await database.observer.get_workflow_run_blocks(workflow_run_id=RUN, organization_id=ORG)
    async with database.Session() as session:
        llm_cost = await session.scalar(
            select(WorkflowRunBlockModel.llm_cost).where(
                WorkflowRunBlockModel.workflow_run_block_id == agent_row.workflow_run_block_id
            )
        )
    assert float(llm_cost) == pytest.approx(MODEL_CALL_COST)
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
    for tool in agent_step_loop.AGENT_BLOCK_TOOLS.values():
        if isinstance(tool, agent_step_loop._BlockTool):
            schema = agent_step_loop._arguments_schema(tool, state)
            references = set(re.findall(r'"#/\$defs/(\w+)"', json.dumps(schema)))
            assert references == set(schema.get("$defs", {})), tool.yaml_class.__name__


def test_every_block_type_is_an_agent_tool_or_says_why_not() -> None:
    assert set(agent_step_loop.AGENT_BLOCK_TOOLS) == set(BlockType)
    for block_type, tool in agent_step_loop.AGENT_BLOCK_TOOLS.items():
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
    monkeypatch: pytest.MonkeyPatch, database: AgentDB, ignore: bool
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    run_context = app.WORKFLOW_CONTEXT_MANAGER.workflow_run_contexts[RUN]
    run_context.inherited_workflow_system_prompt = "Answer in French."
    sent: list[str | None] = []

    async def fake_llm(prompt: str, prompt_name: str, system_prompt: str | None = None, **_: object) -> dict[str, str]:
        if prompt_name == "text-prompt":
            sent.append(system_prompt)
        return {"llm_response": "oui"}

    class Page:
        page = SimpleNamespace(url="https://example.com/")

        async def extract(self, prompt: str, schema: dict[str, Any] | None = None, **kwargs: str | None) -> str:
            # Page AI that no block label is bound to resolves an omitted system prompt to the run-wide one.
            sent.append(kwargs.get("system_prompt", run_context.resolve_effective_workflow_system_prompt()))
            return "oui"

    async def create(browser_session_id: str | None = None) -> Page:
        return Page()

    monkeypatch.setattr(app, "SECONDARY_LLM_API_HANDLER", fake_llm)
    monkeypatch.setattr(ScriptSkyvernPage, "create", create)
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


@pytest.mark.asyncio
async def test_page_commands_run_under_one_agent_task_and_reach_page_ai_as_written(
    monkeypatch: pytest.MonkeyPatch, database: AgentDB
) -> None:
    monkeypatch.setitem(LLMConfigRegistry._configs, LUNA_MEDIUM, _pinned("medium"))
    bound: list[tuple[str | None, str | None]] = []

    class Page:
        page = SimpleNamespace(url="https://example.com/")

        async def extract(self, prompt: str, schema: dict[str, Any] | None = None) -> str:
            context = skyvern_context.ensure_context()
            bound.append((context.task_id, context.step_id))
            return _render_template_with_label(prompt)

    async def create(browser_session_id: str | None = None) -> Page:
        return Page()

    monkeypatch.setattr(ScriptSkyvernPage, "create", create)
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
