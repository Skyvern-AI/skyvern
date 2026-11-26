from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from structlog.testing import capture_logs

from skyvern.forge import app
from skyvern.forge.sdk.agents.skills import SKILL_WRITE_TOOL_NAMES, AvailableSkill, SkillSession, render_skills_catalog
from skyvern.forge.sdk.ember import agent as ember_agent
from skyvern.forge.sdk.ember.agent import _build_narrative_payload, _build_user_context, _ember_skill_session
from skyvern.forge.sdk.ember.browser_ablation import resolve_ember_tool_surface
from skyvern.forge.sdk.ember.output_utils import (
    format_tool_result_for_user,
    summarize_tool_result,
    summarize_tool_result_detail,
)
from skyvern.forge.sdk.ember.request_policy import RequestPolicy
from skyvern.forge.sdk.ember.secret_scrub import register_secret_scrub_value
from skyvern.forge.sdk.ember.tools import ember_native_tools, load_skill_tool, read_skill_resource_tool
from skyvern.forge.sdk.ember.tools.agent_skills import delete_org_skill_tool
from skyvern.forge.sdk.ember.tools.skills import load_skill, read_skill_resource
from skyvern.forge.sdk.ember.turn_origin import TurnOrigin
from skyvern.forge.sdk.forge_log import add_log_context
from skyvern.forge.sdk.schemas.ember import EmberChatRequest, EmberStreamMessageType
from tests.unit.conftest import invoke_ember_tool, make_ember_context
from tests.unit.ember_test_helpers import (
    REPLY_ARGUMENTS,
    RecordingSkills,
    ScriptedModel,
    available_skill,
    make_ember_ctx,
    run_scripted_turn,
    scripted_call,
)

LOGIN_BODY = "ORG-BODY-SENTINEL: tick 'Remember this device' at https://example.com/login.\n"


def _context(turn_origin: TurnOrigin = TurnOrigin.interactive):
    ctx = make_ember_context(organization_id="o_skills", workflow_permanent_id="wpid_skills")
    ctx.workflow_copilot_chat_id = "wcc_skills"
    ctx.turn_origin = turn_origin
    return ctx


def _user_context(catalog: str) -> str:
    return _build_user_context("blocks: []", "", "", "", "Draft a login workflow", skills_catalog=catalog)


@pytest.mark.asyncio
async def test_interactive_turn_lists_skills_with_fences_escaped(monkeypatch: pytest.MonkeyPatch) -> None:
    hook = RecordingSkills(
        [available_skill("example-portal-login", "organization", LOGIN_BODY, description="Use ```fenced``` logins.")]
    )
    monkeypatch.setattr(app, "AGENT_FUNCTION", hook)

    session = await _ember_skill_session(_context())
    with capture_logs() as logs:
        rendered = _user_context(render_skills_catalog(session))

    assert hook.calls == [("o_skills", "wpid_skills", "copilot")]
    assert "example-portal-login [organization]: Use ` ` `fenced` ` ` logins." in rendered
    assert rendered.index("AVAILABLE SKILLS:") < rendered.index("USER MESSAGE:")
    assert [log for log in logs if log["event"] == "agent_skill_list_shown"] == [
        {
            "event": "agent_skill_list_shown",
            "log_level": "info",
            "agent": "copilot",
            "count": 1,
            "list_chars": len("example-portal-login [organization]: Use ```fenced``` logins."),
            "organization_id": "o_skills",
            "workflow_copilot_chat_id": "wcc_skills",
        }
    ]


@pytest.mark.parametrize(
    ("skills", "turn_origin"),
    [
        ([], TurnOrigin.interactive),
        ([available_skill("example-portal-login", "organization", LOGIN_BODY)], TurnOrigin.code_block_ai_fallback),
    ],
    ids=["no_skills_or_flag_off", "ai_fallback_turn"],
)
@pytest.mark.asyncio
async def test_no_list_without_skills_or_on_ai_fallback_turns(
    monkeypatch: pytest.MonkeyPatch, skills: list[AvailableSkill], turn_origin: TurnOrigin
) -> None:
    hook = RecordingSkills(skills)
    monkeypatch.setattr(app, "AGENT_FUNCTION", hook)

    session = await _ember_skill_session(_context(turn_origin))
    with capture_logs() as logs:
        catalog = render_skills_catalog(session)

    assert catalog == ""
    assert "AVAILABLE SKILLS" not in _user_context(catalog)
    assert not [log for log in logs if log["event"].startswith("agent_skill_")]
    if turn_origin == TurnOrigin.code_block_ai_fallback:
        assert hook.calls == []


def _surface_sha(**kwargs: bool) -> tuple[str, tuple[str, ...]]:
    surface = resolve_ember_tool_surface(
        mode=None,
        native_tools=ember_native_tools(
            supports_question_tool=True, browser_code_available=False, run_tools_available=True, **kwargs
        ),
        alias_map={},
        overlays={},
    )
    return surface.sha256, surface.ordered_native_names


def test_skill_tools_join_the_surface_only_when_skills_are_available() -> None:
    default_sha, default_names = _surface_sha()
    with_sha, with_names = _surface_sha(skills_available=True)

    assert "load_skill" not in default_names and "read_skill_resource" not in default_names
    assert {"load_skill", "read_skill_resource"} <= set(with_names)
    assert with_sha != default_sha
    # A client that cannot show the approval card (the Ember MCP client, which answers cards itself) gets no writes.
    offered = {
        card: SKILL_WRITE_TOOL_NAMES & set(_surface_sha(skill_writes_available=True, supports_skill_write_card=card)[1])
        for card in (True, False)
    }
    assert offered == {True: SKILL_WRITE_TOOL_NAMES, False: set()}


@pytest.mark.asyncio
async def test_a_turn_with_skills_sends_the_tools_and_the_list_together(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict[str, Any] = {}

    async def capture_model_loop(**kwargs: Any) -> None:
        sent["tools"] = {tool.name for tool in kwargs["native_tools"]}
        sent["input"] = json.dumps(kwargs["initial_input"], default=str)
        raise RuntimeError("model call stubbed out in tests")

    async def default_policy(*_args: object, **_kwargs: object) -> RequestPolicy:
        return RequestPolicy()

    hook = RecordingSkills([available_skill("example-portal-login", "organization", LOGIN_BODY)])
    monkeypatch.setattr(app, "AGENT_FUNCTION", hook)
    monkeypatch.setattr(ember_agent, "build_request_policy_trust_floor", default_policy)
    monkeypatch.setattr(ember_agent, "_run_agent_loop_with_surface", capture_model_loop)

    await ember_agent.run_ember_agent(
        stream=object(),
        organization_id="o_skills",
        chat_request=EmberChatRequest(
            workflow_permanent_id="wpid_skills",
            workflow_id="w_skills",
            workflow_copilot_chat_id="wcc_skills",
            message="Draft a login workflow",
            workflow_yaml="",
        ),
        chat_history=[],
        global_llm_context=None,
        llm_api_handler=None,
    )

    assert {"load_skill", "read_skill_resource"} <= sent["tools"]
    assert "AVAILABLE SKILLS" in sent["input"] and "example-portal-login [organization]" in sent["input"]


def _tool_ctx(*skills: AvailableSkill):
    ctx = _context()
    ctx.skill_session = SkillSession(
        skills, agent="copilot", organization_id="o_skills", workflow_copilot_chat_id="wcc_skills"
    )
    return ctx


@pytest.mark.asyncio
async def test_tools_return_ok_results_and_errors_as_data() -> None:
    org = available_skill("example-portal-login", "organization", LOGIN_BODY)
    from_runs = replace(available_skill("from-runs", "organization", LOGIN_BODY), files_withheld=True)
    ctx = _tool_ctx(org, available_skill("shared-helper", "everyone", LOGIN_BODY), from_runs)

    loaded = await invoke_ember_tool(load_skill_tool, ctx, {"name": "example-portal-login"})
    repeat = await invoke_ember_tool(load_skill_tool, ctx, {"name": "example-portal-login"})
    read = await invoke_ember_tool(read_skill_resource_tool, ctx, {"name": "shared-helper", "path": "SKILL.md"})
    unknown = await invoke_ember_tool(load_skill_tool, ctx, {"name": "nope"})
    missing = await invoke_ember_tool(read_skill_resource_tool, ctx, {"name": "example-portal-login", "path": "x"})

    assert loaded == {
        "ok": True,
        "data": {
            "name": "example-portal-login",
            "version": org.version,
            "body": LOGIN_BODY,
            "files": [],
            "owner": "organization",
        },
    }
    assert repeat["data"]["status"] == "already_loaded" and "body" not in repeat["data"]
    assert read["ok"] is True and read["data"]["path"] == "SKILL.md"
    assert unknown["ok"] is False and unknown["data"] == {
        "reason": "unknown_skill",
        "names": ["example-portal-login", "from-runs"],
    }
    assert missing["data"] == {"reason": "unknown_file", "files": ["SKILL.md"]}
    for tool, args in (
        (load_skill_tool, {"name": "from-runs"}),
        (read_skill_resource_tool, {"name": "from-runs", "path": "SKILL.md"}),
    ):
        withheld = await invoke_ember_tool(tool, ctx, args)
        assert withheld["ok"] is False and withheld["data"]["reason"] == "withheld"
        assert LOGIN_BODY not in json.dumps(withheld)

    fallback = _tool_ctx(available_skill("example-portal-login", "organization", LOGIN_BODY))
    fallback.turn_origin = TurnOrigin.code_block_ai_fallback
    refused = await invoke_ember_tool(load_skill_tool, fallback, {"name": "example-portal-login"})
    assert refused["ok"] is False
    assert LOGIN_BODY not in json.dumps(refused)


EVERYONE_BODY = "EVERYONE-BODY-SENTINEL: shared notes for https://example.com.\n"
FILE_TEXT = "FILE-TEXT-SENTINEL"


@pytest.mark.asyncio
async def test_user_surfaces_never_show_skill_text_or_everyone_names() -> None:
    ctx = make_ember_ctx(organization_id="o_skills", workflow_permanent_id="wpid_skills", stream=None)
    ctx.skill_session = SkillSession(
        [
            available_skill("example-portal-login", "organization", LOGIN_BODY, **{"reference/form.md": FILE_TEXT}),
            available_skill("shared-helper", "everyone", EVERYONE_BODY, **{"notes/secret-path.md": FILE_TEXT}),
        ],
        agent="copilot",
    )
    register_secret_scrub_value(ctx, "hunter2-secret")
    calls = [
        scripted_call("load_skill", {"name": "example-portal-login"}, "call_org"),
        scripted_call(
            "load_skill",
            {"name": "shared-helper", "user_facing_reason": "Loading the shared-helper skill."},
            "call_everyone",
        ),
        scripted_call(
            "read_skill_resource", {"name": "example-portal-login", "path": "reference/form.md"}, "call_read"
        ),
        scripted_call(
            "read_skill_resource",
            {
                "name": "shared-helper",
                "path": "notes/secret-path.md",
                "user_facing_reason": "Reading notes/secret-path.md from shared-helper.",
            },
            "call_read_e",
        ),
        scripted_call("load_skill", {"name": "nope"}, "call_unknown"),
        scripted_call("delete_org_skill", {"name": "hunter2-secret", "scope": "organization"}, "call_delete"),
        scripted_call(
            "load_skill",
            {"name": "shared-helper [everyone]", "user_facing_reason": "Loading the shared-helper skill."},
            "call_catalog_form",
        ),
    ]
    model = ScriptedModel([calls, [scripted_call("reply", REPLY_ARGUMENTS, "call_reply")]])

    tools = [load_skill_tool, read_skill_resource_tool, delete_org_skill_tool]
    _, ctx, stream = await run_scripted_turn(model, tools=tools, ctx=ctx)

    frames = [
        frame
        for frame in stream.sent
        if frame.type in (EmberStreamMessageType.TOOL_CALL, EmberStreamMessageType.TOOL_RESULT)
    ]
    inputs = {f.tool_call_id: f.tool_input for f in frames if f.type == EmberStreamMessageType.TOOL_CALL}
    results = {f.tool_call_id: f for f in frames if f.type == EmberStreamMessageType.TOOL_RESULT}
    assert inputs["call_org"] == {"name": "example-portal-login"}
    assert inputs["call_everyone"] == {"name": "a Skyvern skill"}
    assert inputs["call_read"] == {"name": "example-portal-login", "path": "reference/form.md"}
    assert inputs["call_read_e"] == {"name": "a Skyvern skill"}
    assert inputs["call_catalog_form"] == {"name": "a Skyvern skill"}
    assert inputs["call_delete"] == {"scope": "organization"}
    assert results["call_org"].summary == "Loaded skill example-portal-login"
    assert results["call_everyone"].summary == "Loaded a Skyvern skill"
    assert results["call_read"].summary == "Read reference/form.md from skill example-portal-login"
    assert results["call_read_e"].summary == "Read a file from a Skyvern skill"
    assert (results["call_unknown"].summary, results["call_unknown"].detail) == ("Could not load that skill",) * 2

    payload = _build_narrative_payload(ctx, terminal="response", terminal_message=None, narrative_summary=None)
    persisted = json.dumps(payload, default=str)
    shown = json.dumps([frame.model_dump(mode="json") for frame in frames]) + persisted
    for leaked in ("BODY-SENTINEL", FILE_TEXT, "shared-helper", "secret-path", "hunter2"):
        assert leaked not in shown
    assert "Loaded skill example-portal-login" in persisted


def test_formatters_show_fixed_text_for_every_skill_failure() -> None:
    names = {"ok": False, "error": "No skill is named 'x'.", "data": {"reason": "unknown_skill", "names": ["org-only"]}}
    files = {"ok": False, "error": "x has no file 'y'.", "data": {"reason": "unknown_file", "files": ["secret.md"]}}
    for tool_name, text in (
        ("load_skill", "Could not load that skill"),
        ("read_skill_resource", "Could not read that skill file"),
    ):
        for result in (names, files):
            assert summarize_tool_result(tool_name, result) == text
            assert format_tool_result_for_user(tool_name, result) == text
            assert summarize_tool_result_detail(result, tool_name=tool_name) == text


@pytest.mark.asyncio
async def test_skill_events_carry_the_request_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    org = available_skill("example-portal-login", "organization", LOGIN_BODY)
    monkeypatch.setattr(app, "AGENT_FUNCTION", RecordingSkills([org]))
    session = await _ember_skill_session(_context())

    with capture_logs() as logs:
        render_skills_catalog(session)
        load_skill(session, "example-portal-login")
        load_skill(session, "example-portal-login")
        read_skill_resource(session, "example-portal-login", "SKILL.md")
        load_skill(session, "nope")

    events = [log for log in logs if log["event"].startswith("agent_skill_")]
    assert [log["event"] for log in events] == [
        "agent_skill_list_shown",
        "agent_skill_loaded",
        "agent_skill_loaded",
        "agent_skill_resource_read",
        "agent_skill_loaded",
    ]
    loaded = [log for log in events if log["event"] == "agent_skill_loaded"]
    assert [(log["already_loaded"], log["error"], log["chars"]) for log in loaded] == [
        (False, None, len(LOGIN_BODY)),
        (True, None, 0),
        (False, "unknown_skill", 0),
    ]
    # capture_logs skips the processor chain, so pass the event through the production context processor.
    shipped = add_log_context(None, "info", dict(loaded[0]))
    assert (shipped["name"], shipped["owner"], shipped["skill_version"]) == (
        "example-portal-login",
        "organization",
        org.version,
    )
    read = next(log for log in events if log["event"] == "agent_skill_resource_read")
    assert (read["path"], read["error"]) == ("SKILL.md", None)
    for log in events:
        assert (log["agent"], log["organization_id"], log["workflow_copilot_chat_id"]) == (
            "copilot",
            "o_skills",
            "wcc_skills",
        )
