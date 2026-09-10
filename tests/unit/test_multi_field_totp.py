"""Focused tests for the page-armed, one-burst multi-field TOTP flow."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from ast import literal_eval
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from skyvern.config import settings
from skyvern.forge.agent import (
    ForgeAgent,
    _first_plan_carries_consumable_totp,
    _multi_field_totp_box_group,
)
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import (
    MultiFieldTotpAttempt,
    SkyvernContext,
    action_for_multi_field_totp_persistence,
)
from skyvern.forge.sdk.models import StepStatus
from skyvern.schemas.run_enums import RunEngine
from skyvern.webeye.actions.actions import ActionStatus, ClickAction, InputOrSelectContext, InputTextAction
from skyvern.webeye.actions.handler import (
    _fill_multi_field_totp_group,
    _resolve_multi_field_totp_code,
)
from skyvern.webeye.actions.responses import STALE_TARGET_TOOL_RESULT, ActionFailure, ActionSuccess
from tests.unit.helpers import make_organization, make_step, make_task
from tests.unit.scoped_asyncio import ScopedAsyncio

_SEED = "JBSWY3DPEHPK3PXP"
_HINT = "907182"
_CODE = "650294"


def _has_playwright_browser() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


_skip_no_browser = pytest.mark.skipif(
    not _has_playwright_browser(),
    reason="Requires Playwright browsers installed (run: playwright install chromium)",
)


def _log_fields(record: logging.LogRecord) -> dict[str, object]:
    if isinstance(record.msg, dict):
        return record.msg
    message = re.sub(r"\x1b\[[0-9;]*m", "", record.getMessage())
    fields: dict[str, object] = {}
    for key, value in re.findall(
        r"""['"]?(\w+)['"]?\s*[:=]\s*('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|\[[^\]]*\]|[^\s,}]+)""",
        message,
    ):
        try:
            fields[key] = literal_eval(value)
        except (ValueError, SyntaxError):
            fields[key] = {"true": True, "false": False, "null": None}.get(value, value)
    return fields


def _input(element_id: str, *, input_type: str = "text", frame: str = "frame-a") -> dict:
    return {
        "id": element_id,
        "tagName": "input",
        "frame": frame,
        "attributes": {"type": input_type, "maxlength": "1", "inputmode": "numeric"},
        "children": [],
    }


def _flatten(nodes: list[dict]) -> list[dict]:
    flattened: list[dict] = []
    for node in nodes:
        flattened.append(node)
        flattened.extend(_flatten(node.get("children", [])))
    return flattened


def _box_page(
    count: int,
    *,
    parent_tag: str = "form",
    parent_id: str = "otp-scope",
    input_type: str = "text",
    frame: str = "frame-a",
) -> SimpleNamespace:
    inputs = [_input(f"box-{index}", input_type=input_type, frame=frame) for index in range(count)]
    parent = {
        "id": parent_id,
        "tagName": parent_tag,
        "frame": frame,
        "attributes": {},
        "children": inputs,
    }
    tree = [parent]
    return SimpleNamespace(elements=_flatten(tree), element_tree=tree, id_to_frame_dict={})


@pytest.mark.parametrize("expected_digits", [4, 6, 8])
def test_group_predicate_accepts_exact_lengths(expected_digits: int) -> None:
    page = _box_page(expected_digits)

    assert _multi_field_totp_box_group(page, expected_digits) == [f"box-{index}" for index in range(expected_digits)]


def test_group_predicate_accepts_password_and_frame_metadata() -> None:
    page = _box_page(6, input_type="password", frame="child-frame")

    assert _multi_field_totp_box_group(page, 6) == [f"box-{index}" for index in range(6)]


def test_group_predicate_uses_nearest_container_and_ignores_unrelated_numeric_input() -> None:
    page = _box_page(6, parent_tag="div")
    unrelated = _input("unrelated", input_type="number")
    unrelated_scope = {
        "id": "unrelated-scope",
        "tagName": "div",
        "frame": "frame-a",
        "attributes": {},
        "children": [unrelated],
    }
    page.elements.append(unrelated)
    page.element_tree.append(unrelated_scope)

    assert _multi_field_totp_box_group(page, 6) == [f"box-{index}" for index in range(6)]


def test_group_predicate_rejects_multiple_groups_and_body_only_scope() -> None:
    first = _box_page(6, parent_tag="div", parent_id="first-scope")
    second = _box_page(6, parent_tag="div", parent_id="second-scope")
    page = SimpleNamespace(
        elements=[*first.elements, *second.elements],
        element_tree=[*first.element_tree, *second.element_tree],
        id_to_frame_dict={},
    )
    assert _multi_field_totp_box_group(page, 6) is None

    inputs = [_input(f"body-box-{index}") for index in range(6)]
    body = {
        "id": "body",
        "tagName": "body",
        "frame": "frame-a",
        "attributes": {},
        "children": inputs,
    }
    assert (
        _multi_field_totp_box_group(
            SimpleNamespace(elements=[body, *inputs], element_tree=[body], id_to_frame_dict={}), 6
        )
        is None
    )


def test_group_predicate_fails_closed_for_duplicate_ids_and_malformed_tree() -> None:
    page = _box_page(6)
    page.elements.append(dict(page.elements[1]))
    assert _multi_field_totp_box_group(page, 6) is None

    duplicate_tree_page = _box_page(6)
    duplicate_tree_page.element_tree[0]["children"].append(dict(duplicate_tree_page.elements[1]))
    assert _multi_field_totp_box_group(duplicate_tree_page, 6) is None

    malformed_page = SimpleNamespace(elements=["not-a-node"], element_tree=[], id_to_frame_dict={})
    assert _multi_field_totp_box_group(malformed_page, 6) is None


def test_group_predicate_walks_saved_flat_tree_fixture() -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "otp_page_elements.json"
    tree = json.loads(fixture_path.read_text(encoding="utf-8"))
    page = SimpleNamespace(elements=_flatten(tree), element_tree=tree, id_to_frame_dict={})

    assert _multi_field_totp_box_group(page, 6) == [f"fixture-box-{index}" for index in range(6)]


def _attempt() -> MultiFieldTotpAttempt:
    return MultiFieldTotpAttempt(
        box_element_ids=[f"box-{index}" for index in range(6)],
        expected_digits=6,
        code_source="secret",
        hint_code=_HINT,
    )


class _CredentialContext:
    def __init__(self, secret: str) -> None:
        self.secret = secret
        self.values = {"credential": {"totp": "credential-token"}}
        self.secrets = {"credential-token_value": secret}

    def represent_plaintext_secrets_as_placeholders(self, payload: object) -> object:
        return deepcopy(payload)

    def totp_secret_value_key(self, placeholder: str) -> str:
        return f"{placeholder}_value"

    def get_original_secret_value_or_none(self, key: str) -> str | None:
        return self.secrets.get(key)


@pytest.mark.parametrize("expected_digits", [6, 8])
@pytest.mark.parametrize("exhaust_collisions", [False, True])
@pytest.mark.parametrize("code_source", ["secret", "external", "seed_before_arming"])
@pytest.mark.parametrize("payload_shape", ["dict", "list"])
def test_secret_navigation_payload_stashes_secret_and_replaces_model_value(
    monkeypatch: pytest.MonkeyPatch,
    expected_digits: int,
    exhaust_collisions: bool,
    code_source: str,
    payload_shape: str,
) -> None:
    from skyvern.forge import agent as agent_module

    task_id = "task-navigation-secret"
    goal_literal, extraction_literal, payload_literal = (digit * expected_digits for digit in "123")
    payload = {
        "credential": {"totp": "credential-token"},
        "again": {"credential": {"totp": "credential-token"}},
        "literal": "0" + payload_literal + "9",
    }
    supplied_code = (_CODE + "73")[:expected_digits]
    if code_source == "external":
        payload = {"verification_code": supplied_code, **payload}
    if payload_shape == "list":
        payload = [payload]
    secret = f"otpauth://totp/Test?secret={_SEED}&digits={expected_digits}"
    workflow_context = _CredentialContext(secret)
    decoy = (_HINT + "39")[:expected_digits]
    next_decoy = decoy[::-1]
    decoys = iter(
        [goal_literal] * 8
        if exhaust_collisions
        else [goal_literal, extraction_literal, payload_literal, decoy, next_decoy]
    )
    monkeypatch.setattr(agent_module, "_generate_multi_field_totp_hint", lambda digits: next(decoys))
    manager = SimpleNamespace(
        workflow_run_contexts={"workflow": workflow_context},
        get_workflow_run_context=lambda _workflow_run_id: workflow_context,
    )
    monkeypatch.setattr(agent_module.app, "WORKFLOW_CONTEXT_MANAGER", manager)
    task = SimpleNamespace(
        complete_criterion=None,
        terminate_criterion=None,
        task_id=task_id,
        workflow_run_id="workflow",
        navigation_payload=payload,
        navigation_goal="Use " + goal_literal,
        data_extraction_goal="Retain " + extraction_literal,
    )
    context = SkyvernContext(task_id=task_id, workflow_run_id="workflow")
    if code_source == "seed_before_arming":
        context.totp_codes[task_id] = supplied_code
        context.seed_generated_totp_values[task_id] = {supplied_code}

    with skyvern_context.scoped(context):
        agent = ForgeAgent()
        result = agent._build_navigation_payload(task, step=SimpleNamespace(), scraped_page=_box_page(expected_digits))
        if exhaust_collisions:
            assert task_id not in context.multi_field_totp
            assert f"{task_id}_secret" not in context.totp_codes
            assert task.navigation_payload == payload
            return
        state = context.multi_field_totp[task_id]
        expected = deepcopy(payload)
        credential_payload = expected[0] if payload_shape == "list" else expected
        credential_payload["credential"]["totp"] = decoy
        credential_payload["again"]["credential"]["totp"] = decoy
        if code_source == "external" or payload_shape == "dict":
            credential_payload["verification_code"] = decoy
        if payload_shape == "list":
            expected.append(str({"verification_code": decoy}))
        assert result == expected
        assert supplied_code not in json.dumps(result)
        assert task.navigation_payload == payload
        assert state.hint_code == decoy
        assert state.credential_placeholders == frozenset({"credential-token"})
        assert context.totp_codes[f"{task_id}_secret"] == secret
        assert state.box_element_ids == [f"box-{index}" for index in range(expected_digits)]
        assert state.code_source == ("secret" if code_source == "seed_before_arming" else code_source)
        if code_source == "external":
            assert context.totp_codes[f"{task_id}_totp_cache"] == supplied_code
        again = agent._build_navigation_payload(task, step=SimpleNamespace(), scraped_page=_box_page(expected_digits))
        assert again == result
        assert state.hint_code == decoy
        context.clear_multi_field_totp_state(task_id)
        agent._build_navigation_payload(task, step=SimpleNamespace(), scraped_page=_box_page(expected_digits))
        assert context.multi_field_totp[task_id].hint_code == next_decoy
        assert context.multi_field_totp[task_id].credential_placeholders == frozenset({"credential-token"})


@pytest.mark.parametrize(
    "payload",
    [
        {"verification_code": "654321", "nested": {"note": "code 654321; account A6543219"}},
        "verification_code: 654321; account A6543219",
        [{"verification_code": "654321"}, "code 654321; account A6543219"],
    ],
)
@pytest.mark.asyncio
@pytest.mark.parametrize("use_caching", [False, True])
async def test_external_stash_survives_pop_missing_code_and_id_only_refresh(
    monkeypatch: pytest.MonkeyPatch, payload: object, use_caching: bool
) -> None:
    task_id = "task-navigation-external"
    from skyvern.forge import agent as agent_module

    monkeypatch.setattr(agent_module, "_generate_multi_field_totp_hint", lambda digits: _HINT)
    original_payload = deepcopy(payload)
    now = datetime.now(UTC)
    task = make_task(
        now,
        make_organization(now),
        task_id=task_id,
        url="https://example.com/code/654321",
        navigation_payload=payload,
        navigation_goal="Enter code 654321 for A6543219; references X654321 and 654321Z; phone 16543219.",
        data_extraction_goal="Report code 654321",
        complete_criterion="The boxes contain 654321 and Verify has been clicked.",
        terminate_criterion="Code 654321 is rejected.",
        error_code_mapping={"otp_rejected": "The site rejected 654321."},
        llm_key="test",
    )
    original_task = task.model_dump()
    step = make_step(now, task, step_id="step", status=StepStatus.created, order=0, output=None)
    page = _box_page(6)
    page.last_used_element_tree_html = None
    page.build_element_tree = MagicMock(return_value="<form></form>")
    page.build_lean_elements_tree = MagicMock(return_value="<form></form>")
    browser_state = SimpleNamespace(get_working_page=AsyncMock(return_value=None))
    context = SkyvernContext(task_id=task_id, totp_codes={task_id: "654321"})
    agent = ForgeAgent()
    monkeypatch.setattr(agent, "_get_action_results", AsyncMock(return_value="Action A6543219 entered 654321."))
    monkeypatch.setattr(
        agent,
        "_get_prompt_caching_settings",
        AsyncMock(return_value={agent_module.EXTRACT_ACTION_TEMPLATE: use_caching}),
    )
    monkeypatch.setattr(agent, "_is_multi_tab_control_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(agent_module, "get_slim_output_template_value", AsyncMock(return_value=False))
    monkeypatch.setattr(agent_module, "build_open_tabs_context", AsyncMock(return_value=None))
    monkeypatch.setattr(
        agent_module.app,
        "EXPERIMENTATION_PROVIDER",
        SimpleNamespace(is_feature_enabled_cached=AsyncMock(return_value=False)),
    )
    monkeypatch.setattr(
        agent_module.app.AGENT_FUNCTION,
        "get_extra_extract_action_guidance",
        AsyncMock(return_value="Handle code 654321 carefully."),
    )
    monkeypatch.setattr(
        agent_module.app,
        "WORKFLOW_CONTEXT_MANAGER",
        SimpleNamespace(workflow_run_contexts={}, get_secret_values_for_run=lambda _: []),
    )

    render = MagicMock(wraps=agent_module.prompt_engine.load_prompt)
    monkeypatch.setattr(agent_module.prompt_engine, "load_prompt", render)

    with skyvern_context.scoped(context):
        result = agent._build_navigation_payload(
            task, expire_verification_code=True, step=SimpleNamespace(), scraped_page=_box_page(6)
        )
        assert "A6543219" in json.dumps(result)
        assert f"A{_HINT}9" not in json.dumps(result)
        assert '"654321"' not in json.dumps(result)
        assert "code 654321;" not in json.dumps(result)
        assert "code: 654321;" not in json.dumps(result)
        assert _HINT in json.dumps(result)
        assert task.navigation_payload == original_payload
        assert task_id not in context.totp_codes
        built = await agent._build_extract_action_prompt(task, step, browser_state, page)
        assert built.use_caching is use_caching
        assert "A6543219" in built.prompt
        assert "X654321 and 654321Z" in built.prompt
        assert "phone 16543219" in built.prompt
        assert f"Enter code {_HINT}" in built.prompt
        assert f"Report code {_HINT}" in built.prompt
        assert f"The boxes contain {_HINT} and Verify has been clicked." in built.prompt
        assert "The site rejected 654321." in built.prompt
        assert f"Handle code {_HINT} carefully." in built.prompt
        assert "https://example.com/code/654321" in built.prompt  # nosemgrep: incomplete-url-substring-sanitization
        rendered = {entry.args[0]: entry.kwargs for entry in render.call_args_list}
        assert set(rendered) == (
            {"extract-action-static", "extract-action-dynamic"} if use_caching else {"extract-action"}
        )
        for kwargs in rendered.values():
            assert kwargs["complete_criterion"] == f"The boxes contain {_HINT} and Verify has been clicked."
            assert kwargs["terminate_criterion"] == f"Code {_HINT} is rejected."
            assert kwargs["starting_url"] == task.url
            assert kwargs["current_url"] == task.url
            assert kwargs["action_history"] == "Action A6543219 entered 654321."
            assert kwargs["error_code_mapping_str"] == json.dumps(task.error_code_mapping)
            assert kwargs["navigation_goal"] == (
                f"Enter code {_HINT} for A6543219; references X654321 and 654321Z; phone 16543219."
            )
            assert "A6543219" in kwargs["navigation_payload_str"]
            assert f"A{_HINT}9" not in kwargs["navigation_payload_str"]
        assert task.model_dump() == original_task
        assert task.navigation_goal == original_task["navigation_goal"]
        assert task.data_extraction_goal == "Report code 654321"
        assert task.navigation_payload == original_payload
        assert "654321" in context.runtime_secret_values
        context.runtime_secret_values.discard("654321")
        page.html = "<form></form>"
        page.id_to_css_dict = {}
        page.element_tree_trimmed = page.element_tree

        def prepare_artifacts(**kwargs):
            assert "654321" in context.runtime_secret_values

        monkeypatch.setattr(agent_module.app.ARTIFACT_MANAGER, "accumulate_scrape_to_archive", prepare_artifacts)
        await agent._persist_scrape_artifacts(task=task, step=step, scraped_page=page, context=context)
        state = context.multi_field_totp[task_id]
        state.filled_code_hash = hashlib.sha256(b"654321").hexdigest()
        state.filled_at = 10.0
        context.pop_totp_code(task_id)
        assert context.totp_codes[f"{task_id}_totp_cache"] == "654321"

        task.navigation_payload = {}
        refreshed_page = _box_page(6)
        for index, element in enumerate(refreshed_page.element_tree[0]["children"]):
            element["id"] = f"refreshed-box-{index}"
        agent._build_navigation_payload(task, step=SimpleNamespace(), scraped_page=refreshed_page)

    assert context.multi_field_totp[task_id] is state
    assert state.box_element_ids == [f"refreshed-box-{index}" for index in range(6)]
    assert state.filled_code_hash == hashlib.sha256(b"654321").hexdigest()
    assert state.filled_at == 10.0

    assert context.totp_codes[f"{task_id}_totp_cache"] == "654321"

    from skyvern.webeye.actions import handler

    next_task = task.model_copy(
        update={
            "navigation_payload": {},
            "navigation_goal": "Enter the verification code.",
            "data_extraction_goal": "Report completion.",
            "complete_criterion": "Verification is complete.",
            "terminate_criterion": "Verification failed.",
            "error_code_mapping": {},
            "url": "https://example.com/verification",
        }
    )
    next_page = deepcopy(page)
    next_page.element_tree = []
    next_page.element_tree_trimmed = []
    monkeypatch.setattr(agent, "_get_action_results", AsyncMock(return_value="Entered the code."))
    monkeypatch.setattr(
        agent_module.app.AGENT_FUNCTION,
        "get_extra_extract_action_guidance",
        AsyncMock(return_value="Check verification."),
    )
    for fill_status in ("verified", "unverified", "not_delivered"):
        for hint_present in (True, False):
            next_state = MultiFieldTotpAttempt(list(state.box_element_ids), 6, "external", hint_code=_HINT)
            if fill_status == "verified":
                handler._record_multi_field_totp_fill(next_state, "654321")
            elif fill_status == "unverified":
                handler._multi_field_totp_unverified_success(next_state, "654321")
            next_context = SkyvernContext(
                task_id=task_id,
                multi_field_totp={task_id: next_state},
                totp_codes={f"{task_id}_totp_cache": "654321", **({task_id: _HINT} if hint_present else {})},
            )
            with skyvern_context.scoped(next_context):
                next_prompt = await agent._build_extract_action_prompt(next_task, step, browser_state, next_page)
            assert ("654321" in next_prompt.prompt) is (fill_status == "unverified")
            assert task_id not in next_context.multi_field_totp
            assert f"{task_id}_totp_cache" not in next_context.totp_codes


@_skip_no_browser
@pytest.mark.asyncio
@pytest.mark.parametrize("use_caching", [False, True])
async def test_multi_field_totp_browser_privacy_after_fill(monkeypatch: pytest.MonkeyPatch, use_caching: bool) -> None:
    from skyvern.forge import agent as agent_module

    task_id = "task-browser-privacy"
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id=task_id, llm_key="test")
    step = make_step(now, task, step_id="step", status=StepStatus.created, order=0, output=None)
    state = MultiFieldTotpAttempt(
        box_element_ids=[f"box-{index}" for index in range(6)],
        expected_digits=6,
        code_source="external",
        hint_code=_HINT,
        filled_code_hash=hashlib.sha256(b"654321").hexdigest(),
        filled_at=10.0,
    )
    context = SkyvernContext(task_id=task_id, totp_codes={f"{task_id}_totp_cache": "654321"})
    context.multi_field_totp[task_id] = state
    context.runtime_secret_values.add("654321")
    agent = ForgeAgent()
    monkeypatch.setattr(agent_module, "_generate_multi_field_totp_hint", lambda digits: _HINT)
    monkeypatch.setattr(
        agent,
        "_get_prompt_caching_settings",
        AsyncMock(return_value={agent_module.EXTRACT_ACTION_TEMPLATE: use_caching}),
    )
    monkeypatch.setattr(agent, "_is_multi_tab_control_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(agent_module, "get_slim_output_template_value", AsyncMock(return_value=False))
    monkeypatch.setattr(agent_module, "build_open_tabs_context", AsyncMock(return_value=None))
    monkeypatch.setattr(
        agent_module.app,
        "EXPERIMENTATION_PROVIDER",
        SimpleNamespace(is_feature_enabled_cached=AsyncMock(return_value=False)),
    )
    monkeypatch.setattr(
        agent_module.app,
        "WORKFLOW_CONTEXT_MANAGER",
        SimpleNamespace(workflow_run_contexts={}, get_secret_values_for_run=lambda _: []),
    )

    # Privacy is a document property: exercise real serialization and capture after a real fill.
    from bs4 import BeautifulSoup
    from playwright.async_api import async_playwright

    from skyvern.forge.taskv3.pre_submit_capture import PreSubmitCaptureRing
    from skyvern.forge.taskv3.tools import build_browser_tools, observe_js
    from skyvern.webeye.actions import handler
    from skyvern.webeye.actions.multi_field_totp import (
        _multi_field_totp_box_groups,
        _multi_field_totp_container_identity,
        _multi_field_totp_structural_identity,
    )
    from skyvern.webeye.scraper.scraped_page import ElementTreeFormat, ScrapedPage
    from skyvern.webeye.scraper.scraper import build_element_dict, hash_element, trim_element_tree
    from skyvern.webeye.utils.dom import DomUtil, SkyvernElement
    from skyvern.webeye.utils.page import SkyvernFrame, mask_otp_values_in_html

    privacy_task = task.model_copy(
        update={
            "url": "about:blank",
            "navigation_payload": {},
            "navigation_goal": "Enter the verification code.",
            "data_extraction_goal": "Report the result.",
            "complete_criterion": "Verification accepted.",
            "terminate_criterion": "Verification rejected.",
            "error_code_mapping": None,
        }
    )
    monkeypatch.setattr(agent, "_get_action_results", AsyncMock(return_value=""))
    monkeypatch.setattr(
        agent_module.app.AGENT_FUNCTION, "get_extra_extract_action_guidance", AsyncMock(return_value="")
    )
    monkeypatch.setattr(settings, "ENABLE_SECRET_VISUAL_MASKING", False)
    context.max_screenshot_scrolls = 0
    markup = (
        '<section id="privacy">Details<form unique_id="otp-scope" id="otp-scope">Verification code'
        + "".join(f'<input unique_id="box-{i}" id="box-{i}" maxlength="1" inputmode="numeric">' for i in range(6))
        + '</form><div><div><div><div><input unique_id="unrelated" id="unrelated" maxlength="32" value="1234">'
        + "</div></div></div></div></section>"
    )
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True, args=["--use-mock-keychain", "--password-store=basic"]
        )
        try:
            live_page = await browser.new_page()
            await live_page.set_content(markup)
            await live_page.evaluate("""() => {
                const boxes = [...document.querySelectorAll('#otp-scope input')];
                boxes.forEach((box, i) => box.addEventListener('input', () => {
                    box.setAttribute('value', box.value);
                    if (box.value && boxes[i + 1]) boxes[i + 1].focus();
                }));
            }""")
            browser_state = SimpleNamespace(
                get_working_page=AsyncMock(return_value=live_page),
                engine_selection=None,
                take_post_action_screenshot=AsyncMock(return_value=b"screenshot"),
            )
            with skyvern_context.scoped(context):
                frame = await SkyvernFrame.create_instance(live_page)
                elements, tree, _ = await frame.build_tree_from_body("main.frame", 0)
                css, by_id, frames, hashes, hash_ids = build_element_dict(elements)
                fresh = ScrapedPage(
                    elements=elements,
                    element_tree=tree,
                    element_tree_trimmed=trim_element_tree(deepcopy(tree)),
                    id_to_css_dict=css,
                    id_to_element_dict=by_id,
                    id_to_frame_dict=frames,
                    id_to_element_hash=hashes,
                    hash_to_element_ids=hash_ids,
                    url="about:blank",
                    _browser_state=browser_state,
                    _clean_up_func=AsyncMock(),
                    _scrape_exclude=None,
                )
                state.box_element_ids = [f"box-{i}" for i in range(6)]
                identity = _multi_field_totp_container_identity(_multi_field_totp_box_groups(fresh, 6)[0])
                box_identities = [
                    _multi_field_totp_structural_identity(by_id[element_id]) for element_id in state.box_element_ids
                ]
                filled = await _fill_multi_field_totp_group(live_page, fresh, privacy_task, state, "654321")
                assert filled.success and filled.data == {"totp_group_filled": True}
                assert await live_page.locator("[data-skyvern-otp-box]").count() == 6
                assert await live_page.locator("html").get_attribute("data-skyvern-otp-filled") == "6"
                assert "654321" in context.runtime_secret_values
                captured_html = {}
                monkeypatch.setattr(
                    agent_module.app.ARTIFACT_MANAGER,
                    "accumulate_screenshot_to_step_archive",
                    MagicMock(return_value=[]),
                )
                monkeypatch.setattr(
                    agent_module.app.ARTIFACT_MANAGER,
                    "accumulate_action_html_to_archive",
                    lambda **kwargs: captured_html.update(kwargs),
                )
                await agent.record_artifacts_after_action(
                    privacy_task,
                    step,
                    browser_state,
                    RunEngine.skyvern_v1,
                    InputTextAction(element_id="box-0", text=_HINT),
                )
                action_html = BeautifulSoup(captured_html["html_action"].decode(), "html.parser")
                assert [box.get("value") for box in action_html.select("#otp-scope input")] == ["*"] * 6
                assert action_html.select_one("#unrelated")["value"] == "1234"
                assert await live_page.locator("#box-0").input_value() == "6"
                for shape in ("same_nodes", "scoped", "remounted", "cleared_task", "new_document"):
                    if shape == "scoped":
                        await live_page.locator("#otp-scope input").evaluate_all(
                            "boxes => boxes.forEach(box => box.removeAttribute('data-skyvern-otp-box'))"
                        )
                    if shape == "remounted":
                        await live_page.locator("#otp-scope").evaluate("""scope => {
                            scope.outerHTML = '<form id="otp-scope">Verification code' + [...scope.querySelectorAll('input')].map((box, i) =>
                                '<input id="remount-' + i + '" maxlength="1" inputmode="numeric" value="' + box.value + '">'
                            ).join('') + '<input inputmode="numeric" value=""></form>';
                        }""")
                    if shape == "cleared_task":
                        context.clear_multi_field_totp_state(task_id)
                        context.task_id = None
                    if shape == "new_document":
                        await live_page.goto("about:blank")
                        await live_page.set_content(
                            '<input unique_id="box-0" id="unrelated" maxlength="32" value="1234">'
                        )
                        frame = await SkyvernFrame.create_instance(live_page)
                    elements, tree, _ = await frame.build_tree_from_body("main.frame", 0)
                    css, by_id, frames, hashes, hash_ids = build_element_dict(elements)
                    fresh = ScrapedPage(
                        elements=elements,
                        element_tree=tree,
                        element_tree_trimmed=trim_element_tree(deepcopy(tree)),
                        id_to_css_dict=css,
                        id_to_element_dict=by_id,
                        id_to_frame_dict=frames,
                        id_to_element_hash=hashes,
                        hash_to_element_ids=hash_ids,
                        html=await frame.get_content(),
                        url="about:blank",
                        _browser_state=browser_state,
                        _clean_up_func=AsyncMock(),
                        _scrape_exclude=None,
                    )
                    parsed = BeautifulSoup(fresh.html, "html.parser")
                    assert parsed.select_one("#unrelated")["value"] == "1234"
                    if shape == "new_document":
                        assert by_id["box-0"]["attributes"]["value"] == "1234"
                        continue
                    boxes = [
                        item
                        for item in elements
                        if str(item.get("attributes", {}).get("id", "")).startswith(("box-", "remount-"))
                    ]
                    assert len(boxes) == 6
                    assert [box["attributes"]["value"] for box in boxes] == ["*"] * 6
                    assert [box.get("value") for box in parsed.select("#otp-scope input")[:6]] == ["*"] * 6
                    for box, digit in zip(boxes, "654321"):
                        raw = deepcopy(box)
                        raw["attributes"]["value"] = digit
                        assert hashes[box["id"]] == hash_element(box) != hash_element(raw)
                    if shape == "same_nodes":
                        assert (
                            _multi_field_totp_container_identity(_multi_field_totp_box_groups(fresh, 6)[0]) == identity
                        )
                        assert [
                            _multi_field_totp_structural_identity(by_id[element_id])
                            for element_id in state.box_element_ids
                        ] == box_identities
                        prefilled = await _fill_multi_field_totp_group(live_page, fresh, privacy_task, state, "654321")
                        assert prefilled.data == {"totp_group_prefilled": True}
                    if shape == "same_nodes":
                        ring = PreSubmitCaptureRing(AsyncMock(return_value=live_page), None)
                        await ring.capture("click", {"selector": "#submit"})
                        assert len(ring.frames) == 1
                        pre_submit = BeautifulSoup(ring.frames[0].html.decode(), "html.parser")
                        assert [box.get("value") for box in pre_submit.select("#otp-scope input")] == ["*"] * 6
                    native_tools = build_browser_tools(AsyncMock(return_value=live_page))
                    get_html = next(tool for tool in native_tools if tool.name == "get_html")
                    for arguments in ({}, {"selector": "#otp-scope"}):
                        native_html = await get_html.handler(arguments)
                        assert native_html.status == "ok"
                        parsed_native = BeautifulSoup(native_html.content, "html.parser")
                        assert len([box for box in parsed_native.find_all("input") if box.get("value") == "*"]) == 6
                    native = json.loads(await live_page.evaluate(observe_js()))
                    native_boxes = [
                        item
                        for item in native["elements"]
                        if item.get("selector", "").startswith(("#box-", "#remount-"))
                    ]
                    assert len(native_boxes) == 6
                    assert all(
                        item.get("value") == "(hidden)" and item["label"] not in list("654321") for item in native_boxes
                    )
                    if shape == "cleared_task":
                        assert _multi_field_totp_box_group(fresh, 6) is None  # seven candidates after rerender
                        # The actual input-context path must serialize the live ancestor subtree.
                        dom = DomUtil(page=live_page, scraped_page=fresh)
                        unrelated = await dom.get_skyvern_element_by_id("unrelated")
                        context_llm = AsyncMock(return_value={})
                        monkeypatch.setattr(
                            handler,
                            "app",
                            SimpleNamespace(
                                AGENT_FUNCTION=agent_module.app.AGENT_FUNCTION, PARSE_SELECT_LLM_API_HANDLER=context_llm
                            ),
                        )
                        monkeypatch.setattr(handler, "get_org_aware_secondary_llm_api_handler", lambda **_: context_llm)
                        monkeypatch.setattr(handler, "get_slim_output_template_value", AsyncMock(return_value=False))
                        monkeypatch.setattr(
                            handler.app.AGENT_FUNCTION,
                            "cleanup_element_tree_factory",
                            lambda **_: AsyncMock(side_effect=lambda _frame, _url, tree: tree),
                        )
                        await handler._get_input_or_select_context(
                            handler.AbstractActionForContextParse(
                                element_id="unrelated", reasoning=None, intention=None
                            ),
                            unrelated,
                            ScrapedPage(
                                elements=[],
                                element_tree=[],
                                element_tree_trimmed=[],
                                _browser_state=None,
                                _clean_up_func=None,
                                _scrape_exclude=None,
                            ),
                            step,
                            engine_selection=None,
                        )
                        context_html = BeautifulSoup(context_llm.call_args.kwargs["prompt"], "html.parser")
                        assert len([box for box in context_html.find_all("input") if box.get("value") == "*"]) == 6
                    if shape in ("same_nodes", "scoped"):
                        captured_scrape = {}
                        monkeypatch.setattr(
                            agent_module.app.ARTIFACT_MANAGER,
                            "accumulate_scrape_to_archive",
                            lambda **kwargs: captured_scrape.update(kwargs),
                        )
                        await agent._persist_scrape_artifacts(
                            task=privacy_task, step=step, scraped_page=fresh, context=context
                        )
                        for key in ("element_tree", "element_tree_trimmed"):
                            saved_tree = json.loads(captured_scrape[key])
                            assert [
                                node["attributes"]["value"]
                                for node in _flatten(saved_tree)
                                if node.get("tagName") == "input" and node.get("attributes", {}).get("maxlength") == "1"
                            ] == ["*"] * 6
                        for key in ("html", "element_tree_in_prompt"):
                            saved_html = BeautifulSoup(captured_scrape[key].decode(), "html.parser")
                            assert len([box for box in saved_html.find_all("input") if box.get("value") == "*"]) == 6
                        context.enable_lean_element_tree = use_caching
                        masked_prompt = await agent._build_extract_action_prompt(
                            privacy_task, step, browser_state, fresh
                        )
                        assert _HINT in masked_prompt.prompt
                        for rendering in (
                            fresh.build_element_tree,
                            fresh.build_economy_elements_tree,
                            fresh.build_lean_elements_tree,
                        ):
                            rendered = BeautifulSoup(rendering(ElementTreeFormat.HTML), "html.parser")
                            assert len([box for box in rendered.find_all("input") if box.get("value") == "*"]) == 6
                # Rebuilt boxes can contain whole codes or fragments; value length is not eligibility.
                reflected = ["value", "aria-valuenow", "aria-valuetext", "data-value", "defaultValue", "placeholder"]
                values = ["654321", "54", "4", "3", "2", "1"]
                await live_page.set_content(
                    '<form id="rebuilt">Code'
                    + "".join(
                        f'<input id="secret-{i}" maxlength="1" '
                        + " ".join(f'{name}="{value}"' for name in reflected)
                        + ">"
                        for i, value in enumerate(values)
                    )
                    + '<input id="username" value="1234"><input id="email" type="email" value="a@example.com"></form>'
                    + '<form id="quantity-form"><input id="quantity" inputmode="numeric" value="3"></form>'
                )
                await live_page.locator("html").evaluate("el => el.setAttribute('data-skyvern-otp-filled', '6')")
                frame = await SkyvernFrame.create_instance(live_page)
                elements, _, _ = await frame.build_tree_from_body("main.frame", 0)
                parsed = BeautifulSoup(await frame.get_content(), "html.parser")
                for item in elements:
                    attrs = item.get("attributes", {})
                    if attrs.get("id", "").startswith("secret-"):
                        index = int(attrs["id"].split("-")[1])
                        for name in reflected:
                            assert attrs[name.lower()] == "*" * len(values[index])
                            assert parsed.select_one(f"#secret-{index}")[name.lower()] == "*" * len(values[index])
                    elif attrs.get("id") in {"username", "email", "quantity"}:
                        assert (
                            attrs["value"]
                            == {"username": "1234", "email": "a@example.com", "quantity": "3"}[attrs["id"]]
                        )
                assert parsed.select_one("#quantity")["value"] == "3"
                assert parsed.select_one("#username")["value"] == "1234"
                assert parsed.select_one("#email")["value"] == "a@example.com"
                native_tools = build_browser_tools(AsyncMock(return_value=live_page))
                look = next(tool for tool in native_tools if tool.name == "look")
                looked = await look.handler({})
                assert looked.status == "ok"
                legend = looked.content.splitlines()[1:7]
                assert len(legend) == 6
                assert [line.split("input ", 1)[1] for line in legend] == [repr("*" * len(value)) for value in values]
                # The same sanitized labels are retained for subsequent mark actions.
                look_state = dict(
                    zip(look.handler.__code__.co_freevars, [cell.cell_contents for cell in look.handler.__closure__])
                )
                assert [look_state["_look_manifest"][i]["label"] for i in range(1, 7)] == [
                    "*" * len(value) for value in values
                ]
                from skyvern.services.script_reviewer_v3.skills.interact import _dom_hash, _handler_live_get_dom

                get_html = next(tool for tool in native_tools if tool.name == "get_html")
                for selector in (None, "#rebuilt", "#secret-0"):
                    result = await _handler_live_get_dom({"selector": selector}, SimpleNamespace(page=live_page))
                    assert result.status == "ok"
                    cached = BeautifulSoup(result.data["html"], "html.parser")
                    selected = cached.select('[id^="secret-"]')
                    assert len(selected) == (1 if selector == "#secret-0" else 6)
                    for item in selected:
                        index = int(item["id"].split("-")[1])
                        assert all(item[name.lower()] == "*" * len(values[index]) for name in reflected)
                    native_html = await get_html.handler({"selector": selector})
                    native_parsed = BeautifulSoup(native_html.content, "html.parser")
                    assert native_parsed.select_one("#secret-0")["value"] == "******"
                from skyvern.forge.sdk.copilot.composition_browser_expressions import (
                    COMPOSITION_STRIPPED_HTML_EXPRESSION,
                )
                from skyvern.forge.sdk.copilot.tools.locator_inspection import inspect_locator_matches

                copilot_html = BeautifulSoup(
                    await live_page.evaluate(COMPOSITION_STRIPPED_HTML_EXPRESSION), "html.parser"
                )
                assert copilot_html.select_one("#secret-0")["value"] == "******"
                inspected = await inspect_locator_matches(live_page, ["#secret-0"])
                assert (
                    BeautifulSoup(inspected["selectors"][0]["matches"][0]["outer_html"], "html.parser").input[
                        "aria-valuenow"
                    ]
                    == "******"
                )
                await live_page.locator('[id^="secret-"]').evaluate_all(
                    "inputs => inputs.forEach(input => input.removeAttribute('data-skyvern-otp-box'))"
                )
                page_hash = await live_page.evaluate(agent_module._PAGE_FINGERPRINT_PROBE_JS)
                await live_page.evaluate("""() => {
                    for (const input of document.querySelectorAll('[id^="secret-"]')) {
                        input.setAttribute('data-skyvern-otp-box', '1');
                        input.setAttribute('data-skyvern-otp-trace', 'bookkeeping');
                    }
                    document.querySelector('#rebuilt').setAttribute('data-skyvern-otp-filled', '6');
                }""")
                assert await live_page.evaluate(agent_module._PAGE_FINGERPRINT_PROBE_JS) == page_hash
                safe_hash = await _dom_hash(live_page)
                await live_page.locator("#secret-0").evaluate(
                    "el => { for (const name of ['value','aria-valuenow','aria-valuetext','data-value','defaultvalue','placeholder']) el.setAttribute(name, '123456'); }"
                )
                assert await _dom_hash(live_page) == safe_hash
                assert await live_page.evaluate(agent_module._PAGE_FINGERPRINT_PROBE_JS) == page_hash
                await live_page.locator("#secret-0").evaluate("el => el.setAttribute('placeholder', 'Digit')")
                assert (
                    BeautifulSoup(await frame.get_content(), "html.parser").select_one("#secret-0")["placeholder"]
                    == "Digit"
                )
                for attr_value in (None, "", "0"):
                    for placeholder, expected in (
                        ("6", "*"),
                        ("654321", "******"),
                        ("Digit 1", "Digit 1"),
                        ("Code", "Code"),
                    ):
                        value_attr = "" if attr_value is None else f' value="{attr_value}"'
                        markup = f'<input data-skyvern-otp-box="1" placeholder="{placeholder}"{value_attr}>'
                        parsed = BeautifulSoup(mask_otp_values_in_html(markup), "html.parser")
                        assert parsed.input["placeholder"] == expected
                        await live_page.set_content(markup)
                        await live_page.locator("input").evaluate("el => { el.value = '6'; }")
                        assert BeautifulSoup(await frame.get_content(), "html.parser").input["placeholder"] == expected

                await live_page.set_content(
                    '<form id="mixed">Code<input id="hidden-box" style="display:none" maxlength="1" value="6"><input maxlength="1" value="5"><div id="mixed-host"></div></form>'
                )
                await live_page.evaluate("""() => {
                    document.documentElement.setAttribute('data-skyvern-otp-filled', '6');
                    document.querySelector('#mixed-host').attachShadow({mode:'open'}).innerHTML =
                        '<input maxlength="1" value="4"><input maxlength="1" value="3"><input maxlength="1" value="2"><input maxlength="1" value="1">';
                }""")
                mixed_elements, _, _ = await frame.build_tree_from_body("main.frame", 0)
                mixed_inputs = [item for item in mixed_elements if item["tagName"] == "input"]
                assert len(mixed_inputs) == 5
                assert all(item["attributes"]["value"] == "*" for item in mixed_inputs)
                assert await live_page.locator('[data-skyvern-otp-box="1"]').count() == 6
                assert await live_page.locator("#hidden-box").get_attribute("data-skyvern-otp-box") == "1"
                light_boxes = BeautifulSoup(await frame.get_content(), "html.parser").select("#mixed input")
                assert len(light_boxes) == 2 and all(box["value"] == "*" for box in light_boxes)
                await live_page.evaluate("""() => {
                    for (const input of document.querySelectorAll('#mixed input')) input.removeAttribute('data-skyvern-otp-box');
                    window.globalDomDepthMap = new Map();
                }""")
                await frame.get_incremental_element_tree(wait_until_finished=False)
                assert await live_page.locator("#hidden-box").get_attribute("data-skyvern-otp-box") == "1"
                assert BeautifulSoup(await frame.get_content(), "html.parser").select_one("#hidden-box")["value"] == "*"
                await live_page.set_content('<form>Code<div id="progressive-host"></div></form>')
                await live_page.evaluate("""async () => {
                    document.documentElement.setAttribute('data-skyvern-otp-filled', '6');
                    const shadow = document.querySelector('#progressive-host').attachShadow({mode:'open'});
                    shadow.innerHTML = '<span>Ready</span>';
                    window.globalDomDepthMap = new Map();
                    window.globalListnerFlag = true;
                    window.globalParsedElementCounter = new SafeCounter();
                    window.globalHoverStylesMap = await getHoverStylesMap();
                    const wrapper = document.createElement('div');
                    wrapper.innerHTML = '<input id="progressive-0" maxlength="1" value="6" aria-valuenow="6" aria-valuetext="6" data-value="6" defaultvalue="6" placeholder="6">';
                    shadow.appendChild(wrapper);
                    await addIncrementalNodeToMap(shadow, [wrapper]);
                }""")
                assert await live_page.evaluate("""() => {
                    const pending = [...window.globalDomDepthMap.values()].flat();
                    while (pending.length) {
                        const node = pending.pop();
                        if (node.attributes.id === 'progressive-0') return node.attributes.value === '6';
                        pending.push(...node.children);
                    }
                    return false;
                }""")
                assert await live_page.locator("#progressive-0").get_attribute("data-skyvern-otp-box") is None
                await live_page.evaluate("""async () => {
                    const shadow = document.querySelector('#progressive-host').shadowRoot;
                    const siblings = [];
                    for (let i = 1; i < 6; i++) {
                        const input = document.createElement('input');
                        input.id = `progressive-${i}`;
                        input.maxLength = 1;
                        for (const name of ['value','aria-valuenow','aria-valuetext','data-value','defaultvalue','placeholder'])
                            input.setAttribute(name, String(6 - i));
                        shadow.appendChild(input);
                        siblings.push(input);
                    }
                    await addIncrementalNodeToMap(shadow, siblings);
                }""")
                incremental_elements, incremental_tree = await frame.get_incremental_element_tree()
                for representation in (incremental_elements, incremental_tree):
                    pending = list(representation)
                    boxes = {}
                    while pending:
                        item = pending.pop()
                        if item["tagName"] == "input":
                            assert all(item["attributes"][name.lower()] == "*" for name in reflected)
                            boxes[item["id"]] = item
                        pending.extend(item.get("children", []))
                    assert len(boxes) == 6
                assert await live_page.locator("input").evaluate_all("inputs => inputs.map(input => input.value)") == [
                    "6",
                    "5",
                    "4",
                    "3",
                    "2",
                    "1",
                ]
                await live_page.evaluate("stopGlobalIncrementalObserver()")
                # Iframe fills stamp both the host document and the top document, without task state.
                await live_page.set_content('<iframe id="otp-frame"></iframe>')
                otp_frame = live_page.frames[1]
                await otp_frame.set_content(
                    '<form unique_id="iframe-scope">Code<input unique_id="iframe-box" maxlength="1" value="5"></form>'
                )
                iframe_box = SkyvernElement(
                    otp_frame.locator("input"), otp_frame, {"id": "iframe-box", "tagName": "input"}
                )
                await iframe_box.mark_totp_box(live_page, 6)
                assert await live_page.locator("html").get_attribute("data-skyvern-otp-filled") == "6"
                assert await otp_frame.locator("html").get_attribute("data-skyvern-otp-filled") == "6"
                iframe_capture = await SkyvernFrame.create_instance(otp_frame)
                iframe_elements, _, _ = await iframe_capture.build_tree_from_body("otp-frame", 1)
                assert [item["attributes"]["value"] for item in iframe_elements if item["tagName"] == "input"] == ["*"]
                assert BeautifulSoup(await iframe_capture.get_content(), "html.parser").input["value"] == "*"
                await otp_frame.set_content('<div id="shadow-host" unique_id="shadow-scope"></div>')
                await otp_frame.locator("#shadow-host").evaluate(
                    "host => { host.attachShadow({mode:'open'}).innerHTML = '<input unique_id=shadow-box maxlength=1 value=5>'; }"
                )
                shadow_box = SkyvernElement(
                    otp_frame.locator("input"), otp_frame, {"id": "shadow-box", "tagName": "input"}
                )
                await shadow_box.mark_totp_box(live_page, 6)
                assert await otp_frame.locator("input").get_attribute("data-skyvern-otp-box") == "1"
                await shadow_box.mark_totp_box(live_page, 4)
                await shadow_box.mark_totp_box(live_page, 8)
                assert await otp_frame.locator("html").get_attribute("data-skyvern-otp-filled") == "4"
                assert await live_page.locator("html").get_attribute("data-skyvern-otp-filled") == "4"
                # The shared HTML function also handles self-closing tags and unmarked inputs.
                html = '<html data-skyvern-otp-filled="1"><input id="marked" data-skyvern-otp-box="1" value="6"/><div><input id="unmarked" value="5"></div><input id="flagged" inputmode="numeric" value="4"><input id="empty" maxlength="1" value=""><input id="other" maxlength="32" value="1234"></html>'
                parsed = BeautifulSoup(mask_otp_values_in_html(html), "html.parser")
                assert {box["id"]: box.get("value") for box in parsed.find_all("input")} == {
                    "marked": "*",
                    "unmarked": "5",
                    "flagged": "4",
                    "empty": "",
                    "other": "1234",
                }
        finally:
            await browser.close()


def test_missing_group_disarms_attempt_and_string_stash() -> None:
    task_id = "task-disarm"
    context = SkyvernContext(
        task_id=task_id,
        totp_codes={task_id: "654321", f"{task_id}_secret": _SEED, f"{task_id}_totp_cache": "654321"},
        multi_field_totp={task_id: _attempt()},
    )
    task = SimpleNamespace(
        complete_criterion=None,
        terminate_criterion=None,
        task_id=task_id,
        workflow_run_id=None,
        navigation_payload={},
        navigation_goal=None,
        data_extraction_goal=None,
    )

    with skyvern_context.scoped(context):
        ForgeAgent()._build_navigation_payload(task, step=SimpleNamespace(), scraped_page=_box_page(1))

    assert task_id not in context.multi_field_totp
    assert not context.totp_codes


@pytest.mark.asyncio
@pytest.mark.parametrize("code_source", ["secret", "external"])
async def test_single_input_page_preserves_external_verification_code(
    monkeypatch: pytest.MonkeyPatch, code_source: str
) -> None:
    task_id = "task-single-input"
    task = SimpleNamespace(
        complete_criterion=None,
        terminate_criterion=None,
        task_id=task_id,
        workflow_run_id=None,
        navigation_payload={},
        navigation_goal=None,
        data_extraction_goal=None,
    )
    context = SkyvernContext(task_id=task_id, totp_codes={task_id: "654321"})

    with skyvern_context.scoped(context):
        result = ForgeAgent()._build_navigation_payload(task, step=SimpleNamespace(), scraped_page=_box_page(1))

    assert result == {"verification_code": "654321"}
    assert context.totp_codes == {task_id: "654321"}

    from skyvern.webeye.actions import handler
    from tests.unit.conftest import make_input_element_mock

    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id=task_id, navigation_payload={})
    step = make_step(now, task, step_id="step", status=StepStatus.created, order=0, output=None)
    state = _attempt()
    state.code_source = code_source
    context.multi_field_totp[task_id] = state
    context.totp_codes[f"{task_id}_secret"] = _SEED
    context.totp_codes[f"{task_id}_totp_cache"] = _CODE
    monkeypatch.setattr(handler, "parse_totp_config", lambda _: _FakeTotp())
    monkeypatch.setattr(handler.time, "time", lambda: 40.0)
    element = make_input_element_mock(element_id="single-otp", attrs={"type": "text"})
    written = []

    async def fill(text):
        written.append(text)

    element.input_fill = AsyncMock(side_effect=fill)
    dom = SimpleNamespace(get_skyvern_element_by_id=AsyncMock(return_value=element))
    frame = SimpleNamespace(safe_wait_for_animation_end=AsyncMock())
    incremental = SimpleNamespace(
        start_listen_dom_increment=AsyncMock(),
        stop_listen_dom_increment=AsyncMock(),
        get_incremental_element_tree=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(handler, "DomUtil", lambda *args, **kwargs: dom)
    monkeypatch.setattr(handler.SkyvernFrame, "create_instance", AsyncMock(return_value=frame))
    monkeypatch.setattr(handler, "IncrementalScrapePage", lambda **kwargs: incremental)
    monkeypatch.setattr(handler, "resolve_engine_selection_for_task", lambda *args: None)
    monkeypatch.setattr(
        handler, "get_input_value", AsyncMock(side_effect=lambda *args, **kwargs: written[-1] if written else "")
    )
    monkeypatch.setattr(
        handler, "_get_input_or_select_context", AsyncMock(return_value=InputOrSelectContext(field="OTP"))
    )
    mask = AsyncMock()
    monkeypatch.setattr(handler, "_apply_secret_visual_mask_if_needed", mask)
    burst = AsyncMock(side_effect=AssertionError("A single target must not burst"))
    monkeypatch.setattr(handler, "_fill_multi_field_totp_group", burst)
    action = InputTextAction(element_id="single-otp", text=_HINT)
    page = _box_page(6)
    page.id_to_element_dict = {"single-otp": {"tagName": "input"}}
    with skyvern_context.scoped(context):
        results = await handler.handle_input_text_action(action, MagicMock(), page, task, step)
    assert all(result.success for result in results)
    assert written == [_CODE]
    assert action.text == _HINT
    assert action.totp_timing_info is None
    assert any(entry.args[0] is element and entry.kwargs["is_secret_value"] for entry in mask.await_args_list)


def test_persistence_copy_redacts_armed_digit_and_reasoning_without_mutating_execution_action() -> None:
    metadata = {
        "tagName": "div",
        "attributes": {"value": "7", "class": "otp"},
        "children": [
            {
                "tagName": "input",
                "attributes": {"value": "8"},
                "children": [{"tagName": "span", "attributes": {"value": "9"}, "children": []}],
            },
            {"tagName": "input", "attributes": {"value": ""}, "children": []},
            {"tagName": "input", "attributes": {}, "children": []},
        ],
    }
    original_metadata = deepcopy(metadata)
    action = InputTextAction(
        element_id="box-0",
        text="7",
        reasoning="use the current code here",
        intention="enter the code",
        response="7",
        skyvern_element_data=metadata,
        input_or_select_context=InputOrSelectContext(
            intention="Enter digit 7", field="Box 1", date_format="7", is_required=True
        ),
        totp_timing_info={
            "is_totp_sequence": True,
            "action_index": 0,
            "box_element_ids": ["box-0"],
            "code_source": "external",
            "totp_secret": _SEED,
        },
    )

    persisted = action_for_multi_field_totp_persistence(action)

    assert action.text == "7"
    assert persisted.text == "*"
    assert persisted.reasoning == "Entered a one-time code digit."
    assert persisted.intention == "*"
    assert persisted.response == "*"
    assert persisted.totp_timing_info == {
        "is_totp_sequence": True,
        "action_index": 0,
        "box_element_ids": ["box-0"],
        "code_source": "external",
    }
    assert _SEED not in str(persisted.totp_timing_info)

    assert persisted.input_or_select_context is not None
    assert persisted.input_or_select_context.intention == persisted.reasoning
    assert persisted.input_or_select_context.field == persisted.reasoning
    assert persisted.input_or_select_context.date_format == persisted.reasoning
    assert persisted.input_or_select_context.is_required is True
    assert action.input_or_select_context.intention == "Enter digit 7"

    expected_metadata = deepcopy(original_metadata)
    expected_metadata["attributes"]["value"] = "*"
    expected_metadata["children"][0]["attributes"]["value"] = "*"
    expected_metadata["children"][0]["children"][0]["attributes"]["value"] = "*"
    assert persisted.skyvern_element_data == expected_metadata
    assert action.skyvern_element_data == original_metadata
    assert metadata == original_metadata


class _FakeTotp:
    interval = 30

    def __init__(self) -> None:
        self.at_values: list[int] = []

    def at(self, timestamp: int) -> str:
        self.at_values.append(timestamp)
        return _CODE


@pytest.mark.asyncio
async def test_secret_cache_short_remaining_waits_for_next_window_and_rereads_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = SimpleNamespace(task_id="task-validity", workflow_run_id=None)
    state = _attempt()
    state.valid_from = 90
    state.valid_until = 130
    context = SkyvernContext(
        task_id=task.task_id,
        totp_codes={f"{task.task_id}_secret": _SEED, f"{task.task_id}_totp_cache": "000000"},
        multi_field_totp={task.task_id: state},
    )
    fake_totp = _FakeTotp()
    clock = iter([115.0, 131.0])
    sleep = AsyncMock()
    from skyvern.webeye.actions import handler

    monkeypatch.setattr(handler, "parse_totp_config", lambda _secret: fake_totp)
    monkeypatch.setattr(handler.time, "time", lambda: next(clock))
    monkeypatch.setattr(handler, "asyncio", ScopedAsyncio(sleep=sleep))
    monkeypatch.setattr(settings, "TOTP_MULTI_FIELD_MIN_REMAINING_SECONDS", 20)

    with skyvern_context.scoped(context):
        code = await _resolve_multi_field_totp_code(task, state)

    assert code == _CODE
    assert sleep.await_args_list == [call(15.0)]
    assert fake_totp.at_values == [130]
    assert state.valid_from == 130
    assert state.valid_until == 160


@pytest.mark.asyncio
async def test_short_totp_interval_clamps_minimum_remaining_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    task = SimpleNamespace(task_id="task-short-interval", workflow_run_id=None)
    state = _attempt()
    state.valid_from = 0
    state.valid_until = 15
    context = SkyvernContext(
        task_id=task.task_id,
        totp_codes={f"{task.task_id}_secret": _SEED, f"{task.task_id}_totp_cache": "000000"},
        multi_field_totp={task.task_id: state},
    )
    fake_totp = _FakeTotp()
    fake_totp.interval = 15
    clock = iter([2.0, 15.0])
    sleep = AsyncMock()
    from skyvern.webeye.actions import handler

    monkeypatch.setattr(handler, "parse_totp_config", lambda _secret: fake_totp)
    monkeypatch.setattr(handler.time, "time", lambda: next(clock))
    monkeypatch.setattr(handler, "asyncio", ScopedAsyncio(sleep=sleep))
    monkeypatch.setattr(settings, "TOTP_MULTI_FIELD_MIN_REMAINING_SECONDS", 20)

    with skyvern_context.scoped(context):
        code = await _resolve_multi_field_totp_code(task, state)

    assert code == _CODE
    assert sleep.await_args_list == [call(13.0)]
    assert fake_totp.at_values == [15]
    assert state.valid_from == 15
    assert state.valid_until == 30


@pytest.mark.asyncio
async def test_secret_cache_late_wake_waits_for_following_window(monkeypatch: pytest.MonkeyPatch) -> None:
    task = SimpleNamespace(task_id="task-late-wake", workflow_run_id=None)
    state = _attempt()
    state.valid_from = 0
    state.valid_until = 30
    context = SkyvernContext(
        task_id=task.task_id,
        totp_codes={f"{task.task_id}_secret": _SEED, f"{task.task_id}_totp_cache": "000000"},
        multi_field_totp={task.task_id: state},
    )
    fake_totp = _FakeTotp()
    clock = iter([29.0, 59.0, 60.0])
    sleep = AsyncMock()
    from skyvern.webeye.actions import handler

    monkeypatch.setattr(handler, "parse_totp_config", lambda _secret: fake_totp)
    monkeypatch.setattr(handler.time, "time", lambda: next(clock))
    monkeypatch.setattr(handler, "asyncio", ScopedAsyncio(sleep=sleep))
    monkeypatch.setattr(settings, "TOTP_MULTI_FIELD_MIN_REMAINING_SECONDS", 20)

    with skyvern_context.scoped(context):
        code = await _resolve_multi_field_totp_code(task, state)

    assert code == _CODE
    assert sleep.await_args_list == [call(1.0), call(1.0)]
    assert fake_totp.at_values == [60]
    assert state.valid_from == 60
    assert state.valid_until == 90


@pytest.mark.asyncio
@pytest.mark.parametrize("credential_scope", ["legacy", "allowed", "excluded", "rebound_excluded"])
async def test_expired_secret_cache_regenerates_from_current_window(
    monkeypatch: pytest.MonkeyPatch, credential_scope: str
) -> None:
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="task-expired")
    state = _attempt()
    state.valid_from = 0
    state.valid_until = 10
    context = SkyvernContext(
        task_id=task.task_id,
        totp_codes={f"{task.task_id}_secret": _SEED, f"{task.task_id}_totp_cache": "000000"},
        multi_field_totp={task.task_id: state},
    )
    fake_totp = _FakeTotp()
    sleep = AsyncMock()
    from skyvern.webeye.actions import handler

    monkeypatch.setattr(handler, "parse_totp_config", lambda _secret: fake_totp)
    monkeypatch.setattr(handler.time, "time", lambda: 40.0)
    monkeypatch.setattr(handler, "asyncio", ScopedAsyncio(sleep=sleep))
    log = MagicMock()
    monkeypatch.setattr(handler, "LOG", log)
    expected_code = _CODE
    if credential_scope != "legacy":
        from skyvern.forge.sdk.services.credentials import parse_totp_config
        from tests.unit.test_agent_otp_routing import _real_credential_context

        second_seed = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
        workflow = _real_credential_context(seed=_SEED)
        workflow.parameters["second"] = workflow.parameters["credentials"]
        workflow.values["second"] = {"totp": "second-seed"}
        workflow.secrets[workflow.totp_secret_value_key("second-seed")] = second_seed
        task.workflow_run_id = "wr_test"
        task.navigation_payload = (
            {"credentials": workflow.values["credentials"]} if credential_scope == "excluded" else dict(workflow.values)
        )
        manager = SimpleNamespace(
            workflow_run_contexts={task.workflow_run_id: workflow},
            has_workflow_run_context=lambda _: True,
            get_workflow_run_context=lambda _: workflow,
        )
        monkeypatch.setattr(handler.app, "WORKFLOW_CONTEXT_MANAGER", manager)
        monkeypatch.setattr(handler, "parse_totp_config", parse_totp_config)
        context.clear_multi_field_totp_state(task.task_id)
        context.active_credential_parameter_key = "second" if credential_scope == "excluded" else "credentials"
        with skyvern_context.scoped(context):
            agent = ForgeAgent()
            agent._build_navigation_payload(task, step=SimpleNamespace(), scraped_page=_box_page(6))
            state = context.multi_field_totp[task.task_id]
        assert context.totp_codes[f"{task.task_id}_secret"] == _SEED
        state.valid_from, state.valid_until = 0.0, 120.0
        context.totp_codes[f"{task.task_id}_totp_cache"] = parse_totp_config(_SEED).at(0)
        context.active_credential_parameter_key = "second"
        if credential_scope == "rebound_excluded":
            task.navigation_payload = {"credentials": workflow.values["credentials"]}
            replacement_page = _box_page(6)
            for element in replacement_page.elements:
                if element["tagName"] == "input":
                    element["id"] = "new-" + element["id"]
            with skyvern_context.scoped(context):
                agent._build_navigation_payload(task, step=SimpleNamespace(), scraped_page=replacement_page)
            assert context.multi_field_totp[task.task_id] is state
            assert state.box_element_ids == [f"new-box-{index}" for index in range(6)]
        expected_code = (
            parse_totp_config(second_seed).at(30) if credential_scope == "allowed" else parse_totp_config(_SEED).at(0)
        )

    with skyvern_context.scoped(context):
        code = await _resolve_multi_field_totp_code(task, state)

    assert code == expected_code
    if credential_scope == "legacy":
        assert fake_totp.at_values == [30]
        assert state.credential_placeholders == frozenset()
    else:
        assert context.totp_codes[f"{task.task_id}_secret"] == (second_seed if credential_scope == "allowed" else _SEED)
        assert state.credential_placeholders == (
            frozenset({"cred_totp", "second-seed"}) if credential_scope == "allowed" else frozenset({"cred_totp"})
        )
        if credential_scope != "allowed":
            log.info.assert_called_once_with(
                "Pinned credential is outside the multi-field TOTP attempt scope", task_id=task.task_id
            )
    assert sleep.await_count == 0
    assert context.totp_codes[f"{task.task_id}_totp_cache"] == expected_code
    retained_cache = credential_scope in {"excluded", "rebound_excluded"}
    assert state.valid_from == (0 if retained_cache else 30)
    assert state.valid_until == (120 if retained_cache else 60)


def _fake_group_elements(count: int) -> list[SimpleNamespace]:
    elements: list[SimpleNamespace] = []
    for _index in range(count):
        locator = MagicMock()
        locator.count = AsyncMock(return_value=1)
        frame = SimpleNamespace(is_detached=lambda: False)
        elements.append(
            SimpleNamespace(
                input_fill=AsyncMock(),
                mark_totp_box=AsyncMock(),
                focus=AsyncMock(),
                get_tag_name=lambda: "input",
                get_locator=lambda locator=locator: locator,
                get_frame=lambda frame=frame: frame,
            )
        )
    return elements


def _fill_task() -> SimpleNamespace:
    return SimpleNamespace(task_id="task-fill", workflow_run_id=None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code_source", "remaining", "fallback", "minimum", "interval", "should_refresh"),
    [
        ("secret", 1, False, 20, 30, True),
        ("secret", 9, False, 20, 30, True),
        ("secret", 10, False, 20, 30, False),
        ("secret", 12, False, 20, 30, False),
        ("secret", 9, True, 20, 30, True),
        ("secret", 1, False, 0, 30, True),
        ("external", 5, False, 20, 30, False),
        ("external", 5, True, 20, 30, False),
        ("secret", 6, False, 10, 30, False),
        ("secret", 6, False, 20, 15, True),
        ("secret", 8, False, 20, 15, False),
    ],
)
async def test_group_fill_refreshes_secret_code_when_validity_margin_expires(
    monkeypatch: pytest.MonkeyPatch,
    code_source: str,
    remaining: int,
    fallback: bool,
    minimum: int,
    interval: int,
    should_refresh: bool,
) -> None:
    from skyvern.forge.sdk.services.credentials import parse_totp_config
    from skyvern.webeye.actions import handler

    task = _fill_task()
    state = MultiFieldTotpAttempt([f"box-{index}" for index in range(6)], 6, code_source)
    secret = f"otpauth://totp/Test?secret={_SEED}&digits=6&period={interval}"
    totp = parse_totp_config(secret)
    assert totp is not None and totp.interval == interval
    window_end = 90 + interval
    original_code = totp.at(90)
    next_code = totp.at(window_end)
    assert original_code != next_code
    context = SkyvernContext(
        task_id=task.task_id,
        totp_codes={f"{task.task_id}_secret": secret, f"{task.task_id}_totp_cache": original_code},
    )
    if code_source == "external":
        state.valid_from, state.valid_until = 90.0, float(window_end)
    now = [90.0]
    read_count = 0
    expected_code = next_code if should_refresh else original_code

    async def read_values(_elements: list[SimpleNamespace]) -> list[str]:
        nonlocal read_count
        read_count += 1
        if read_count == 1 or (fallback and read_count == 2):
            if not fallback or read_count == 2:
                now[0] = window_end - remaining
            return [""] * 6
        return list(expected_code)

    async def next_window(**kwargs):
        now[0] = float(window_end)
        return (float(window_end), float(window_end + interval))

    elements = _fake_group_elements(6)
    dom = MagicMock()
    dom.get_skyvern_element_by_id = AsyncMock(side_effect=elements)
    monkeypatch.setattr(handler, "DomUtil", lambda **_: dom)
    monkeypatch.setattr(handler.time, "time", lambda: now[0])
    monkeypatch.setattr(settings, "TOTP_MULTI_FIELD_MIN_REMAINING_SECONDS", minimum)
    wait = AsyncMock(side_effect=next_window)
    monkeypatch.setattr(handler, "_wait_for_next_multi_field_totp_window", wait)
    monkeypatch.setattr(handler, "_apply_secret_visual_mask_if_needed", AsyncMock())
    monkeypatch.setattr(handler, "_read_multi_field_totp_values", read_values)
    log = MagicMock()
    monkeypatch.setattr(handler, "LOG", log)
    page = SimpleNamespace(url="same", keyboard=SimpleNamespace(type=AsyncMock()))

    with skyvern_context.scoped(context):
        initial_code = await _resolve_multi_field_totp_code(task, state)
        result = await _fill_multi_field_totp_group(page, _box_page(6), task, state, initial_code)

    assert initial_code == original_code
    assert isinstance(result, ActionSuccess)
    page.keyboard.type.assert_awaited_once_with(original_code if fallback else expected_code)
    if fallback:
        for element, digit in zip(elements, expected_code):
            element.input_fill.assert_awaited_once_with(digit)
    else:
        assert all(not element.input_fill.await_count for element in elements)
    assert wait.await_count == int(should_refresh)
    assert context.totp_codes[f"{task.task_id}_totp_cache"] == expected_code
    assert state.valid_until == (window_end + interval if should_refresh else window_end)
    external_logs = [
        entry
        for entry in log.info.call_args_list
        if entry.args[0] == "Using supplied multi-field code without regenerating"
    ]
    assert len(external_logs) == int(code_source == "external")
    if external_logs:
        assert external_logs[0].kwargs == {"task_id": task.task_id}


@pytest.mark.parametrize(
    ("missing_box", "initial_values"),
    [(None, list("1234")), (0, list("1234")), (2, list("1234")), (None, list("1239")), (None, ["1234", "", "", ""])],
)
@pytest.mark.asyncio
async def test_group_fill_prefilled_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, missing_box: int | None, initial_values: list[str]
) -> None:
    from skyvern.webeye.actions import handler

    if missing_box is not None:
        from skyvern.webeye.scraper.scraper import build_element_dict

        task = _fill_task()
        scraped_page = _box_page(4, frame="main.frame")
        for index, element in enumerate(scraped_page.element_tree[0]["children"]):
            element["xpath"] = f"/html/body/form/input[{index + 1}]"
        css, by_id, frames, hashes, _ = build_element_dict(scraped_page.elements)
        scraped_page.id_to_css_dict = css
        scraped_page.id_to_element_dict = by_id
        scraped_page.id_to_frame_dict = frames
        scraped_page.id_to_element_hash = hashes
        scraped_page.generate_scraped_page_without_screenshots = AsyncMock()
        page = SimpleNamespace(url="same", keyboard=SimpleNamespace(type=AsyncMock()))
        page.locator = MagicMock(
            side_effect=lambda selector: SimpleNamespace(
                count=AsyncMock(return_value=int(selector != css[f"box-{missing_box}"])),
            )
        )
        state = MultiFieldTotpAttempt(list(css)[1:], 4, "external", hint_code="9071")
        context = SkyvernContext(
            task_id=task.task_id,
            multi_field_totp={task.task_id: state},
            totp_codes={f"{task.task_id}_totp_cache": "1234"},
        )
        action = InputTextAction(element_id="box-0", text="9", totp_timing_info={"is_totp_sequence": True})
        with skyvern_context.scoped(context):
            result = (await handler._handle_input_text_action(action, page, scraped_page, task, SimpleNamespace()))[-1]
        assert isinstance(result, ActionFailure) and result.stop_execution_on_failure and result.skip_remaining_actions
        assert task.task_id not in context.multi_field_totp and not context.totp_codes
        assert not any(entry.args[0].startswith("xpath=") for entry in page.locator.call_args_list)
        page.keyboard.type.assert_not_awaited()
        scraped_page.generate_scraped_page_without_screenshots.assert_not_awaited()
        return

    elements = _fake_group_elements(4)
    dom = MagicMock()
    dom.get_skyvern_element_by_id = AsyncMock(side_effect=elements)
    monkeypatch.setattr(handler, "DomUtil", lambda **_: dom)
    monkeypatch.setattr(handler, "_apply_secret_visual_mask_if_needed", AsyncMock())
    whole_code_in_one_box = initial_values == ["1234", "", "", ""]
    reads = [initial_values, *([initial_values] if whole_code_in_one_box else []), list("1234")]
    monkeypatch.setattr(handler, "_read_multi_field_totp_values", AsyncMock(side_effect=reads))
    page = SimpleNamespace(url="same", keyboard=SimpleNamespace(type=AsyncMock()))
    state = MultiFieldTotpAttempt([f"box-{index}" for index in range(4)], 4, "external")

    result = await _fill_multi_field_totp_group(page, _box_page(4), _fill_task(), state, "1234")

    assert isinstance(result, ActionSuccess)
    if initial_values == list("1234"):
        assert result.data == {"totp_group_prefilled": True}
        page.keyboard.type.assert_not_awaited()
    else:
        assert result.data == {"totp_group_filled": True}
        page.keyboard.type.assert_awaited_once_with("1234")
    if whole_code_in_one_box:
        assert [element.input_fill.await_args.args[0] for element in elements] == list("1234")
    else:
        assert all(not element.input_fill.await_args_list for element in elements)


@pytest.mark.asyncio
async def test_group_fill_stream_success_reads_back_the_whole_code(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.webeye.actions import handler

    elements = _fake_group_elements(4)
    dom = MagicMock()
    dom.get_skyvern_element_by_id = AsyncMock(side_effect=elements)
    reads = AsyncMock(side_effect=[list("0000"), list("1234")])
    monkeypatch.setattr(handler, "DomUtil", lambda **_: dom)
    monkeypatch.setattr(handler, "_apply_secret_visual_mask_if_needed", AsyncMock())
    monkeypatch.setattr(handler, "_read_multi_field_totp_values", reads)
    page = SimpleNamespace(url="same", keyboard=SimpleNamespace(type=AsyncMock()))
    state = MultiFieldTotpAttempt([f"box-{index}" for index in range(4)], 4, "external")

    scraped_page = _box_page(4)
    scraped_page.generate_scraped_page_without_screenshots = AsyncMock()
    result = await _fill_multi_field_totp_group(page, scraped_page, _fill_task(), state, "1234")
    scraped_page.generate_scraped_page_without_screenshots.assert_not_awaited()

    assert isinstance(result, ActionSuccess)
    assert result.data == {"totp_group_filled": True}
    page.keyboard.type.assert_awaited_once_with("1234")
    assert all(not element.input_fill.await_args_list for element in elements)
    assert state.filled_code_hash == hashlib.sha256(b"1234").hexdigest()
    assert state.fill_verified


@pytest.mark.parametrize(
    ("code", "stream_values"), [("1234", list("1111")), ("123456", ["123456", "", "", "", "", ""])]
)
@pytest.mark.asyncio
async def test_group_fill_stream_mismatch_uses_value_replacing_fallback(
    monkeypatch: pytest.MonkeyPatch, code: str, stream_values: list[str]
) -> None:
    from skyvern.webeye.actions import handler

    elements = _fake_group_elements(len(code))
    dom = MagicMock()
    dom.get_skyvern_element_by_id = AsyncMock(side_effect=elements)
    reads = AsyncMock(side_effect=[[""] * len(code), stream_values, list(code)])
    monkeypatch.setattr(handler, "DomUtil", lambda **_: dom)
    monkeypatch.setattr(handler, "_apply_secret_visual_mask_if_needed", AsyncMock())
    monkeypatch.setattr(handler, "_read_multi_field_totp_values", reads)
    page = SimpleNamespace(url="same", keyboard=SimpleNamespace(type=AsyncMock()))
    state = MultiFieldTotpAttempt([f"box-{index}" for index in range(len(code))], len(code), "external")

    result = await _fill_multi_field_totp_group(page, _box_page(len(code)), _fill_task(), state, code)

    assert isinstance(result, ActionSuccess)
    assert page.keyboard.type.await_count == 1
    assert [element.input_fill.await_args.args[0] for element in elements] == list(code)


@pytest.mark.parametrize("final_values", [list("1111"), ["1234", "", "", ""]])
@pytest.mark.asyncio
async def test_group_fill_both_strategies_mismatch_fails_without_reporting_code(
    monkeypatch: pytest.MonkeyPatch,
    final_values: list[str],
) -> None:
    from skyvern.webeye.actions import handler

    elements = _fake_group_elements(4)
    dom = MagicMock()
    dom.get_skyvern_element_by_id = AsyncMock(side_effect=elements)
    reads = AsyncMock(side_effect=[list("0000"), list("1111"), final_values])
    monkeypatch.setattr(handler, "DomUtil", lambda **_: dom)
    monkeypatch.setattr(handler, "_apply_secret_visual_mask_if_needed", AsyncMock())
    monkeypatch.setattr(handler, "_read_multi_field_totp_values", reads)
    page = SimpleNamespace(url="same", keyboard=SimpleNamespace(type=AsyncMock()))
    state = MultiFieldTotpAttempt([f"box-{index}" for index in range(4)], 4, "external")

    result = await _fill_multi_field_totp_group(page, _box_page(4), _fill_task(), state, "1234")

    assert isinstance(result, ActionFailure)
    assert result.exception_message is not None
    assert "1234" not in result.exception_message
    assert "could not be verified" in result.exception_message
    assert state.filled_code_hash is None


@pytest.mark.asyncio
async def test_group_fill_stream_interruption_during_navigation_fails_without_recording_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.webeye.actions import handler

    elements = _fake_group_elements(4)
    dom = MagicMock()
    dom.get_skyvern_element_by_id = AsyncMock(side_effect=elements)
    monkeypatch.setattr(handler, "DomUtil", lambda **_: dom)
    monkeypatch.setattr(handler, "_apply_secret_visual_mask_if_needed", AsyncMock())
    page = SimpleNamespace(url="same", keyboard=SimpleNamespace())

    async def navigate(_code: str) -> None:
        page.url = "next"
        raise RuntimeError("navigation interrupted the stream")

    page.keyboard.type = AsyncMock(side_effect=navigate)
    monkeypatch.setattr(handler, "_read_multi_field_totp_values", AsyncMock(return_value=list("0000")))
    state = MultiFieldTotpAttempt([f"box-{index}" for index in range(4)], 4, "external")

    result = await _fill_multi_field_totp_group(page, _box_page(4), _fill_task(), state, "1234")

    assert isinstance(result, ActionFailure)
    assert "navigation" in (result.exception_message or "")
    assert "interrupted" in (result.exception_message or "")
    assert state.filled_code_hash is None
    assert state.filled_at is None


@pytest.mark.asyncio
async def test_group_fill_read_failure_after_navigation_is_delivered_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.webeye.actions import handler

    elements = _fake_group_elements(4)
    dom = MagicMock()
    dom.get_skyvern_element_by_id = AsyncMock(side_effect=elements)
    reads = AsyncMock(side_effect=[list("0000"), RuntimeError("navigation interrupted the read")])
    monkeypatch.setattr(handler, "DomUtil", lambda **_: dom)
    monkeypatch.setattr(handler, "_apply_secret_visual_mask_if_needed", AsyncMock())
    monkeypatch.setattr(handler, "_read_multi_field_totp_values", reads)
    page = SimpleNamespace(url="same", keyboard=SimpleNamespace())

    async def navigate(_code: str) -> None:
        page.url = "next"

    page.keyboard.type = AsyncMock(side_effect=navigate)
    state = MultiFieldTotpAttempt([f"box-{index}" for index in range(4)], 4, "external")

    result = await _fill_multi_field_totp_group(page, _box_page(4), _fill_task(), state, "1234")

    assert isinstance(result, ActionSuccess)
    assert result.data == {"totp_group_filled": True, "verified": False}
    assert state.filled_code_hash == hashlib.sha256(b"1234").hexdigest()
    assert not state.fill_verified


@pytest.mark.asyncio
async def test_group_fill_fallback_read_failure_after_final_box_navigation_is_delivered_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.webeye.actions import handler

    elements = _fake_group_elements(6)
    dom = MagicMock()
    dom.get_skyvern_element_by_id = AsyncMock(side_effect=elements)
    page = SimpleNamespace(url="same", keyboard=SimpleNamespace(type=AsyncMock()))
    read_count = 0

    async def read_values(_elements: list[SimpleNamespace]) -> list[str]:
        nonlocal read_count
        read_count += 1
        if read_count == 1:
            return list("000000")
        if read_count == 2:
            return list("111111")
        page.url = "next"
        raise RuntimeError("navigation interrupted the read")

    monkeypatch.setattr(handler, "DomUtil", lambda **_: dom)
    monkeypatch.setattr(handler, "_apply_secret_visual_mask_if_needed", AsyncMock())
    monkeypatch.setattr(handler, "_read_multi_field_totp_values", read_values)
    state = MultiFieldTotpAttempt([f"box-{index}" for index in range(6)], 6, "external")

    result = await _fill_multi_field_totp_group(page, _box_page(6), _fill_task(), state, _CODE)

    assert isinstance(result, ActionSuccess)
    assert result.data == {"totp_group_filled": True, "verified": False}
    assert [element.input_fill.await_args.args[0] for element in elements] == list(_CODE)
    assert state.filled_code_hash == hashlib.sha256(_CODE.encode()).hexdigest()
    assert not state.fill_verified


@pytest.mark.asyncio
async def test_group_fill_fallback_navigation_before_final_box_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.webeye.actions import handler

    elements = _fake_group_elements(6)
    page = SimpleNamespace(url="same", keyboard=SimpleNamespace(type=AsyncMock()))

    async def navigate_after_third_box(_value: str) -> None:
        page.url = "next"
        raise RuntimeError("navigation interrupted the fill")

    elements[3].input_fill = AsyncMock(side_effect=navigate_after_third_box)
    dom = MagicMock()
    dom.get_skyvern_element_by_id = AsyncMock(side_effect=elements)
    monkeypatch.setattr(handler, "DomUtil", lambda **_: dom)
    monkeypatch.setattr(handler, "_apply_secret_visual_mask_if_needed", AsyncMock())
    monkeypatch.setattr(
        handler,
        "_read_multi_field_totp_values",
        AsyncMock(side_effect=[list("000000"), list("111111")]),
    )
    state = MultiFieldTotpAttempt([f"box-{index}" for index in range(6)], 6, "external")

    result = await _fill_multi_field_totp_group(page, _box_page(6), _fill_task(), state, _CODE)

    assert isinstance(result, ActionFailure)
    assert "navigation" in (result.exception_message or "")
    assert [element.input_fill.await_args.args[0] for element in elements[:3]] == list(_CODE[:3])
    assert state.filled_code_hash is None


@pytest.mark.parametrize(
    ("phase", "remount_kind"),
    [
        (phase, kind)
        for phase in ("stream", "stream_error", "stream_missing", "fallback")
        for kind in (
            "identical",
            "different",
            "document",
            "unknown_document",
            "unknown_original_document",
            "unknown_fresh_document",
            "fresh_document",
            "fresh_css_miss",
            "gone",
            "ambiguous",
            "partial",
            "omitted_iframe",
            "dashboard",
        )
    ]
    + [
        ("stream", "all_live"),
        ("fallback", "all_live"),
        ("every_digit", "identical"),
        ("fallback", "child-different"),
        ("fallback", "partial_mask"),
        ("stream", "frame_gone"),
        ("stream", "frame_observed_gone"),
        ("stream", "numbered_placeholders"),
        ("fallback", "numbered_placeholders"),
        ("stream", "empty_style"),
        ("fallback", "empty_style"),
        ("fallback", "meaningful_style"),
        ("stream", "replacement_count"),
        ("stream_missing", "replacement_count"),
        ("fallback", "replacement_count"),
        ("stream", "replacement_width"),
        ("fallback", "replacement_width"),
        ("stream", "other_ancestor"),
        ("fallback", "other_ancestor"),
        ("stream_budget", "identical"),
        ("fallback_budget", "identical"),
        ("stream", "scope_ancestor_replacement"),
        ("stream", "scope_ancestor_gone"),
        ("stream", "scope_ancestor_omitted"),
        ("stream", "scope_container_replacement"),
        ("stream", "scope_empty_live"),
        ("stream", "scope_empty_stale"),
    ],
)
@pytest.mark.asyncio
async def test_group_fill_same_page_detach_reresolves_once_then_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, phase: str, remount_kind: str
) -> None:
    from skyvern.webeye.actions import handler
    from skyvern.webeye.scraper.scraped_page import ScrapedPage
    from skyvern.webeye.scraper.scraper import build_element_dict
    from skyvern.webeye.utils.page import SECRET_VISUAL_MASK_ATTRIBUTE, SECRET_VISUAL_MASK_SCRIPT

    caplog.set_level("INFO")
    task = _fill_task()
    changed_ancestor = remount_kind in {"scope_ancestor_replacement", "scope_ancestor_gone", "scope_ancestor_omitted"}
    replacement_count = remount_kind in {
        "replacement_count",
        "scope_ancestor_replacement",
        "scope_container_replacement",
    }
    removed_container = remount_kind in {
        "gone",
        "dashboard",
        "frame_gone",
        "frame_observed_gone",
        "scope_ancestor_gone",
        "scope_empty_stale",
    }
    count = 6 if replacement_count or changed_ancestor else 4
    code = "123456" if count == 6 else "1234"
    hint = "907182" if count == 6 else "9071"
    budget_exhausted = phase in {"stream_budget", "fallback_budget"}
    values = [""] * count
    generation = 0
    read_interrupted = False
    writes = []
    snapshots = []
    locator_requests = []
    iframe_case = remount_kind in {"omitted_iframe", "frame_gone", "frame_observed_gone"}
    box_frame = "otp-frame" if iframe_case else "main.frame"
    raw_tree = _box_page(count, frame=box_frame).element_tree
    raw_tree[0]["attributes"] = (
        {} if remount_kind == "other_ancestor" else {"id": "otp-form", "aria-label": "Verification"}
    )
    for index, element in enumerate(raw_tree[0]["children"]):
        element["attributes"].update({"id": f"otp-digit-{index}", "value": ""})
        if remount_kind in {"empty_style", "meaningful_style"}:
            element["attributes"]["style"] = "" if remount_kind == "empty_style" else "color: red;"
        if remount_kind == "numbered_placeholders":
            element["attributes"]["placeholder"] = str(index + 1)
        element["xpath"] = f"/html/body/form/input[{index + 1}]"
        element["children"] = [
            {"tagName": "span", "frame": "main.frame", "attributes": {"value": "", "role": "note"}, "children": []}
        ]
    dom_attributes = {element["id"]: deepcopy(element["attributes"]) for element in raw_tree[0]["children"]}
    browser_state = SimpleNamespace(engine_selection=None)

    def ids():
        if generation and (
            removed_container or remount_kind in {"omitted_iframe", "scope_ancestor_omitted", "scope_empty_live"}
        ):
            return []
        if generation and remount_kind == "partial_mask":
            return ["box-0", "fresh-1-1", "box-2", "box-3"]
        if generation and remount_kind == "partial":
            return ["box-0", "box-1", "fresh-1-2", "fresh-1-3"]
        return [
            f"box-{index}" if generation == 0 or remount_kind == "all_live" else f"fresh-{generation}-{index}"
            for index in range(
                4
                if generation and replacement_count
                else 1
                if generation and remount_kind == "replacement_width"
                else count
            )
        ]

    def take_snapshot():
        tree = deepcopy(raw_tree)
        if generation and remount_kind in {"scope_empty_live", "scope_empty_stale"}:
            tree[0]["children"] = []
        elif generation and (removed_container or remount_kind in {"omitted_iframe", "scope_ancestor_omitted"}):
            tree = []
        elif generation and remount_kind == "partial":
            tree[0]["children"] = tree[0]["children"][:2]
        elif generation and replacement_count:
            tree[0]["children"] = tree[0]["children"][:4]
        elif generation and remount_kind == "replacement_width":
            tree[0]["children"] = tree[0]["children"][:1]
        for index, element in enumerate(tree[0]["children"] if tree else []):
            element["id"] = ids()[index]
            if element["id"] not in dom_attributes:
                dom_attributes[element["id"]] = deepcopy(raw_tree[0]["children"][index]["attributes"])
            element["attributes"] = deepcopy(dom_attributes[element["id"]])
            if generation and remount_kind == "empty_style":
                element["attributes"].pop("style")
            elif generation and remount_kind == "meaningful_style":
                element["attributes"]["style"] = "color: blue;"
            if generation and remount_kind == "replacement_width":
                element["attributes"]["maxlength"] = "4"
            element["attributes"]["value"] = values[index]
            if generation and remount_kind == "numbered_placeholders":
                element["attributes"]["placeholder"] = "*"
            element["children"][0]["attributes"]["value"] = values[index]
            if generation and remount_kind == "child-different":
                element["children"][0]["attributes"]["role"] = "alert"
            if generation and remount_kind == "different":
                element["attributes"]["name"] = f"unrelated-pin-{index}"
        if generation and remount_kind == "dashboard":
            tree.append(
                {
                    "id": "dashboard",
                    "tagName": "form",
                    "frame": "main.frame",
                    "attributes": {"id": "dashboard"},
                    "children": [_input("dashboard-number", input_type="number", frame="main.frame")],
                }
            )
        if generation and remount_kind == "frame_observed_gone":
            tree.append(
                {"id": "frame-status", "tagName": "div", "frame": "otp-frame", "attributes": {}, "children": []}
            )
        if iframe_case:
            tree = [
                {
                    "id": "otp-frame",
                    "tagName": "iframe",
                    "frame": "main.frame",
                    "attributes": {"id": "otp-iframe"},
                    "children": tree,
                }
            ]
        if generation and remount_kind == "ambiguous":
            second_group = deepcopy(tree[0])
            second_group["id"] = "second-group"
            for index, element in enumerate(second_group["children"]):
                element["id"] = f"second-box-{index}"
            tree.append(second_group)
        if generation and remount_kind in {"identical", "partial_mask"}:
            tree[0]["text"] = "A changing validation message must not change the owning container identity."
        if generation and remount_kind == "scope_container_replacement":
            tree[0]["attributes"]["aria-expanded"] = "true"
        if remount_kind == "other_ancestor" or changed_ancestor:
            tree = [
                {
                    "id": "panel",
                    "tagName": "section",
                    "frame": box_frame,
                    "attributes": {
                        "id": "pin-panel" if generation and remount_kind == "other_ancestor" else "otp-panel",
                        **({"class": "open"} if generation and changed_ancestor else {}),
                    },
                    "children": tree,
                }
            ]
        elements = _flatten(tree)
        css, by_id, frames, hashes, hash_ids = build_element_dict(elements)
        result = ScrapedPage(
            elements=elements,
            element_tree=tree,
            element_tree_trimmed=tree,
            id_to_css_dict=css,
            id_to_element_dict=by_id,
            id_to_frame_dict=frames,
            id_to_element_hash=hashes,
            hash_to_element_ids=hash_ids,
            url="same",
            _browser_state=browser_state,
            _clean_up_func=AsyncMock(),
            _scrape_exclude=None,
            _document_loader_id=(
                None
                if (not generation and remount_kind == "unknown_original_document")
                or (generation and remount_kind == "unknown_fresh_document")
                else "document-after"
                if generation and remount_kind == "fresh_document"
                else "document-before"
            ),
        )
        object.__setattr__(result, "generate_scraped_page_without_screenshots", AsyncMock(side_effect=take_snapshot))
        snapshots.append(result)
        return result

    scraped_page = take_snapshot()
    initial_tree = deepcopy(scraped_page.element_tree)
    initial_ids = ids()
    state = MultiFieldTotpAttempt(
        initial_ids,
        count,
        "external",
        valid_from=1,
        valid_until=100,
        hint_code=hint,
        credential_placeholders=frozenset({"credential-token"}),
    )
    context = SkyvernContext(
        task_id=task.task_id,
        multi_field_totp={task.task_id: state},
        totp_codes={task.task_id: hint, f"{task.task_id}_secret": _SEED, f"{task.task_id}_totp_cache": code},
    )
    before = deepcopy(state)
    code_cache = dict(context.totp_codes)
    page = SimpleNamespace(url="same")
    session = SimpleNamespace(
        send=AsyncMock(
            side_effect=lambda *_: {
                "frameTree": {
                    "frame": {
                        "loaderId": None
                        if generation and remount_kind == "unknown_document"
                        else "document-after"
                        if generation and remount_kind == "document"
                        else "document-before"
                    }
                }
            }
        ),
        detach=AsyncMock(),
    )
    page.context = SimpleNamespace(new_cdp_session=AsyncMock(return_value=session))
    page.main_frame = SimpleNamespace()
    child_frame = SimpleNamespace(
        is_detached=lambda: False,
        frame_element=AsyncMock(return_value=SimpleNamespace(get_attribute=AsyncMock(return_value="otp-frame"))),
        locator=lambda selector: page.locator(selector),
    )
    page.frames = [page.main_frame, child_frame] if iframe_case else [page.main_frame]
    page.query_selector = AsyncMock(return_value=SimpleNamespace(content_frame=AsyncMock(return_value=child_frame)))
    page.frame_locator = lambda selector: page

    async def evaluate_mask(*, expression, arg=None):
        if expression == SECRET_VISUAL_MASK_SCRIPT:
            arg.attributes[SECRET_VISUAL_MASK_ATTRIBUTE] = "true"
            arg.attributes["data-skyvern-observed"] = "true"
        elif 'element.setAttribute("data-skyvern-otp-box", "1")' in expression:
            arg.attributes["data-skyvern-otp-box"] = "1"
        else:
            assert "root.setAttribute('data-skyvern-otp-filled'" in expression

    page.evaluate = AsyncMock(side_effect=evaluate_mask)
    child_frame.evaluate = page.evaluate

    def locator(selector):
        locator_requests.append(selector)
        if selector.startswith("xpath="):
            index = int(selector.rsplit("[", 1)[1].rstrip("]")) - 1
            element_id = ids()[index]
        else:
            element_id = selector.split("'")[1]
            if element_id == "otp-scope":
                return SimpleNamespace(count=AsyncMock(return_value=int(not (generation and removed_container))))
            index = int(element_id.rsplit("-", 1)[1])

        def live():
            return element_id in ids() and not (generation and remount_kind == "fresh_css_miss" and index == 2)

        async def read(**kwargs):
            nonlocal read_interrupted, generation
            if generation and phase in {"stream", "stream_budget"} and not read_interrupted:
                read_interrupted = True
                raise RuntimeError("execution context was destroyed")
            if phase == "stream_budget" and generation == 1 and len(snapshots) == 2 and live():
                generation += 1
                raise RuntimeError("execution context was destroyed")
            if not live():
                if phase == "stream_missing":
                    return None
                raise RuntimeError("execution context was destroyed")
            return values[index]

        async def fill(value, **kwargs):
            nonlocal generation
            if phase == "fallback_budget" and generation < 2 and index == 0:
                generation += 1
                raise RuntimeError("execution context was destroyed")
            if phase == "fallback" and generation == 0 and index == 1:
                generation += 1
                if remount_kind == "other_ancestor":
                    values[:] = ["9"] * count
                raise RuntimeError("execution context was destroyed")
            if not live():
                raise RuntimeError("execution context was destroyed")
            writes.append((element_id, value))
            values[index] = value
            if phase == "every_digit":
                generation += 1

        return SimpleNamespace(
            count=AsyncMock(side_effect=lambda: int(live())),
            input_value=AsyncMock(side_effect=read),
            focus=AsyncMock(),
            fill=AsyncMock(side_effect=fill),
            element_handle=AsyncMock(return_value=SimpleNamespace(attributes=dom_attributes.get(element_id))),
        )

    page.locator = MagicMock(side_effect=locator)

    async def stream(code):
        nonlocal generation
        if phase in {"stream", "stream_error", "stream_missing", "stream_budget"}:
            generation += 1
            if remount_kind == "other_ancestor":
                values[:] = ["9"] * count
            if remount_kind in {"scope_ancestor_replacement", "scope_container_replacement"}:
                values[:4] = list(code[-4:])
            if remount_kind == "frame_gone":
                page.frames = [page.main_frame]
            if phase == "stream_error":
                raise RuntimeError("execution context was destroyed")

    page.keyboard = SimpleNamespace(type=AsyncMock(side_effect=stream))
    if remount_kind == "partial_mask":
        mask = AsyncMock(wraps=handler._apply_secret_visual_mask_if_needed)
        monkeypatch.setattr(settings, "ENABLE_SECRET_VISUAL_MASKING", True)
        monkeypatch.setattr(
            handler.app.WORKFLOW_CONTEXT_MANAGER, "mask_secrets_enabled_for_run", MagicMock(return_value=True)
        )
    else:
        mask = AsyncMock()
    sleep = AsyncMock()
    monkeypatch.setattr(handler, "_apply_secret_visual_mask_if_needed", mask)
    monkeypatch.setattr(handler, "asyncio", ScopedAsyncio(sleep=sleep))
    with skyvern_context.scoped(context):
        result = await _fill_multi_field_totp_group(page, scraped_page, task, state, code)

    assert not any(selector.startswith("xpath=") for selector in locator_requests)
    assert scraped_page.element_tree == initial_tree
    page.keyboard.type.assert_awaited_once_with(code)
    accepted = (
        remount_kind in {"identical", "all_live", "partial_mask", "empty_style", "numbered_placeholders"}
        and not budget_exhausted
    )
    teardown = (
        remount_kind
        in {
            "gone",
            "document",
            "fresh_document",
            "dashboard",
            "frame_gone",
            "frame_observed_gone",
            "scope_ancestor_gone",
        }
        and phase != "fallback"
    )
    if teardown:
        assert writes == []
        assert context.multi_field_totp[task.task_id] is state
        assert context.totp_codes == code_cache
        if phase == "stream_error":
            assert isinstance(result, ActionFailure) and not result.success
            assert result.data is None
            assert "interrupted before completion" in result.exception_message
            assert state.filled_code_hash is None
        else:
            assert isinstance(result, ActionSuccess)
            assert result.data == {"totp_group_filled": True, "verified": False}
            assert state.filled_code_hash == hashlib.sha256(code.encode()).hexdigest()
    elif accepted:
        assert isinstance(result, ActionSuccess)
        assert "".join(values) == code
        assert state.box_element_ids == ids()
        assert all(
            dom_attributes[element_id].get("data-skyvern-otp-box") == "1" for element_id in state.box_element_ids
        )
        assert context.multi_field_totp[task.task_id] is state
        assert context.totp_codes == code_cache
        for attribute in ("hint_code", "valid_from", "valid_until", "credential_placeholders", "code_source"):
            assert getattr(state, attribute) == getattr(before, attribute)
        assert state.filled_code_hash == hashlib.sha256(code.encode()).hexdigest()
        assert len(writes) == count
        assert mask.await_count == count * len(snapshots)
        if remount_kind == "partial_mask":
            assert sum(
                entry.kwargs.get("expression") == SECRET_VISUAL_MASK_SCRIPT for entry in page.evaluate.call_args_list
            ) == count * len(snapshots)
            assert snapshots[1].id_to_element_dict["box-0"]["attributes"][SECRET_VISUAL_MASK_ATTRIBUTE] == "true"
            assert SECRET_VISUAL_MASK_ATTRIBUTE not in snapshots[1].id_to_element_dict["fresh-1-1"]["attributes"]
            assert all(
                attrs[SECRET_VISUAL_MASK_ATTRIBUTE] == "true"
                for element_id, attrs in dom_attributes.items()
                if element_id in ids()
            )
        if phase == "every_digit":
            assert len(snapshots) == count + 1
            assert [element_id for element_id, _ in writes] == ["box-0", "fresh-1-1", "fresh-2-2", "fresh-3-3"]
        else:
            assert len(snapshots) == 2
    else:
        assert isinstance(result, ActionFailure) and not result.success
        assert result.stop_execution_on_failure and result.skip_remaining_actions
        assert result.data == {"totp_group_gone": True, "replan": True}
        assert "widget changed" in result.exception_message and code not in result.exception_message
        assert writes == ([("box-0", "1")] if phase == "fallback" else [])
        assert task.task_id not in context.multi_field_totp
        if phase == "stream_error":
            assert not context.totp_codes
            assert state.filled_code_hash is None
        else:
            assert context.totp_codes == {task.task_id: code}
            assert state.filled_code_hash == hashlib.sha256(code.encode()).hexdigest()
            assert not state.fill_verified
            with skyvern_context.scoped(context):
                next_payload = ForgeAgent()._build_navigation_payload(
                    SimpleNamespace(task_id=task.task_id, workflow_run_id=None, navigation_payload={}),
                    step=SimpleNamespace(),
                    scraped_page=_box_page(0),
                )
            assert next_payload["verification_code"] == code
    assert all(entry == call(0.1) for entry in sleep.await_args_list)
    if not accepted:
        rejection_logs = [
            record for record in caplog.records if "Multi-field OTP binding rejected" in record.getMessage()
        ]
        assert len(rejection_logs) == 1
        assert code not in rejection_logs[0].getMessage() and _SEED not in rejection_logs[0].getMessage()
        fields = _log_fields(rejection_logs[0])
        expected_reason = (
            "recovery_budget_exhausted"
            if budget_exhausted
            else {
                "different": "box_identity_mismatch",
                "document": "document_changed_before_snapshot",
                "fresh_document": "document_changed_after_snapshot",
                "unknown_document": "loader_id_unknown",
                "unknown_original_document": "loader_id_unknown",
                "unknown_fresh_document": "loader_id_unknown",
                "fresh_css_miss": "fresh_css_resolution_miss",
                "gone": "original_widget_scope_gone",
                "ambiguous": "ambiguous_fresh_groups",
                "partial": "original_container_still_live",
                "omitted_iframe": "original_frame_unobserved",
                "dashboard": "original_widget_scope_gone",
                "child-different": "box_identity_mismatch",
                "frame_gone": "original_widget_scope_gone",
                "frame_observed_gone": "original_widget_scope_gone",
                "meaningful_style": "box_identity_mismatch",
                "replacement_count": "replacement_inputs_in_original_scope",
                "replacement_width": "replacement_inputs_in_original_scope",
                "other_ancestor": "replacement_inputs_in_original_scope",
                "scope_ancestor_replacement": "replacement_inputs_in_original_scope",
                "scope_ancestor_gone": "original_widget_scope_gone",
                "scope_ancestor_omitted": "live_container_unobserved",
                "scope_container_replacement": "replacement_inputs_in_original_scope",
                "scope_empty_live": "original_container_still_live",
                "scope_empty_stale": "original_scope_still_present",
            }[remount_kind]
        )
        assert fields["reason_code"] == expected_reason
        assert fields["classification"] == (
            "teardown"
            if expected_reason
            in {"document_changed_before_snapshot", "document_changed_after_snapshot", "original_widget_scope_gone"}
            else "unconfirmed"
        )
        assert fields["loader_id_known"] is (expected_reason != "loader_id_unknown")
        assert {"candidate_groups", "original_frames", "fresh_frames", "live_boxes"} <= fields.keys()
    if budget_exhausted:
        assert len(snapshots) == 2
        assert writes == []
    if remount_kind in {"scope_ancestor_replacement", "scope_container_replacement"}:
        assert values[:4] == list(code[-4:])
        assert state.filled_at is not None
    if remount_kind.startswith("scope_"):
        assert locator_requests.count("[unique_id='otp-scope']") == 1


@pytest.mark.asyncio
async def test_group_fill_same_page_fallback_detach_mismatch_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.webeye.actions import handler

    old_elements = _fake_group_elements(4)
    old_elements[1].input_fill = AsyncMock(side_effect=RuntimeError("execution context was destroyed"))
    replacement_elements = _fake_group_elements(4)
    dom = SimpleNamespace()
    dom.get_skyvern_element_by_id = AsyncMock(side_effect=[*old_elements, *replacement_elements])
    reads = AsyncMock(side_effect=[list("0000"), list("1111"), list("0000")])
    sleep = AsyncMock()
    monkeypatch.setattr(handler, "DomUtil", lambda **_: dom)
    mask = AsyncMock()
    monkeypatch.setattr(handler, "_apply_secret_visual_mask_if_needed", mask)
    monkeypatch.setattr(handler, "_read_multi_field_totp_values", reads)
    monkeypatch.setattr(handler, "asyncio", ScopedAsyncio(sleep=sleep))
    page = SimpleNamespace(url="same", keyboard=SimpleNamespace(type=AsyncMock()))
    refreshed_page = _box_page(4)
    state = MultiFieldTotpAttempt([f"box-{index}" for index in range(4)], 4, "external")

    # Continuity cases use real snapshots above; this case isolates mismatching readback after accepted recovery.
    monkeypatch.setattr(
        handler,
        "_refresh_multi_field_totp_group_binding",
        AsyncMock(return_value=(refreshed_page, state.box_element_ids)),
    )
    result = await _fill_multi_field_totp_group(page, _box_page(4), _fill_task(), state, "1234")

    assert isinstance(result, ActionFailure)
    assert "1234" not in (result.exception_message or "")
    assert sleep.await_args_list == [call(0.1)]
    assert state.filled_code_hash is None
    assert mask.await_count == len(old_elements) + len(replacement_elements)
    assert all(any(entry.args[0] is element for entry in mask.await_args_list) for element in replacement_elements)


def test_context_clear_and_pop_keep_only_scoped_strings() -> None:
    task_id = "task-cleanup"
    state = _attempt()
    assert state.credential_placeholders == frozenset()
    state.credential_placeholders = frozenset({"credential-token"})
    state.filled_code_hash = hashlib.sha256(_CODE.encode()).hexdigest()
    context = SkyvernContext(
        task_id=task_id,
        totp_codes={task_id: _CODE, f"{task_id}_secret": _SEED, f"{task_id}_totp_cache": _CODE},
        multi_field_totp={task_id: state},
    )
    context.pop_totp_code(task_id)
    assert context.totp_codes == {f"{task_id}_secret": _SEED, f"{task_id}_totp_cache": _CODE}
    context.seed_generated_totp_values[task_id] = {_CODE}
    context.seed_generated_totp_values["another-task"] = {_CODE}
    context.clear_multi_field_totp_state(task_id)
    assert task_id not in context.multi_field_totp
    assert not context.totp_codes
    assert context.seed_generated_totp_values == {"another-task": {_CODE}}
    context.multi_field_totp[task_id] = _attempt()
    assert context.multi_field_totp[task_id].credential_placeholders == frozenset()


@pytest.mark.parametrize("filled", [False, True])
@pytest.mark.parametrize(
    "plan_kind",
    [
        "pin",
        "hint",
        "mixed",
        "contradictory",
        "pin-digit",
        "decoy-digit",
        "leading-remount",
        "leading-value-remount",
        "leading-gone",
        "leading-different",
        "leading-other-container",
        "leading-other-ancestor",
        "leading-partial",
        "leading-unchanged",
        "leading-other-frame",
        "leading-navigation",
    ],
)
@pytest.mark.parametrize("code_source", ["secret", "external"])
@pytest.mark.asyncio
async def test_execute_step_rebound_group_requires_expected_value(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    filled: bool,
    plan_kind: str,
    code_source: str,
) -> None:
    from skyvern.forge import agent as agent_module
    from skyvern.forge.sdk.workflow.models.block import ActionBlock
    from skyvern.webeye.actions import handler, multi_field_totp
    from skyvern.webeye.scraper.scraped_page import ScrapedPage
    from skyvern.webeye.scraper.scraper import build_element_dict, hash_element

    task_id = "task-rebind"
    monkeypatch.setattr(agent_module, "_generate_multi_field_totp_hint", lambda digits: _HINT)
    state = _attempt()
    state.code_source = code_source
    if filled:
        state.filled_code_hash = hashlib.sha256(_CODE.encode()).hexdigest()
        state.filled_at = 10.0
        state.valid_from, state.valid_until = 5.0, 35.0
    context = SkyvernContext(
        task_id=task_id,
        multi_field_totp={task_id: state},
        totp_codes={f"{task_id}_secret": _SEED, f"{task_id}_totp_cache": _CODE},
    )
    workflow_context = _CredentialContext(_SEED)
    monkeypatch.setattr(
        agent_module.app,
        "WORKFLOW_CONTEXT_MANAGER",
        SimpleNamespace(
            workflow_run_contexts={"workflow": workflow_context},
            get_workflow_run_context=lambda _workflow_run_id: workflow_context,
            has_workflow_run_context=lambda _workflow_run_id: True,
        ),
    )
    tree = json.loads((Path(__file__).parent / "fixtures" / "otp_page_elements.json").read_text())
    leading = plan_kind.startswith("leading-")
    unchanged = plan_kind == "leading-unchanged"
    rejected = leading and plan_kind not in {"leading-remount", "leading-value-remount", "leading-unchanged"}
    leading_ran = False
    if leading:
        for element in _flatten(tree):
            element["frame"] = "main.frame"
        if plan_kind == "leading-other-ancestor":
            tree[0]["tagName"] = "section"
            tree[0]["attributes"] = {"id": "otp-panel"}
            tree[0]["children"][0]["tagName"] = "form"
            tree[0]["children"][0]["attributes"] = {}
        for index, element in enumerate(tree[0]["children"][0]["children"]):
            element["attributes"].update(
                {
                    "id": f"otp-digit-{index}",
                    "value": ""
                    if unchanged or plan_kind in {"leading-value-remount", "leading-other-ancestor"}
                    else "123456"[index],
                }
            )
        tree[1]["attributes"]["id"] = "verify-code"
        tree.append(
            {
                "id": "open-authenticator",
                "tagName": "button",
                "frame": "main.frame",
                "attributes": {"id": "open-authenticator"},
                "children": [],
            }
        )

    browser_state = SimpleNamespace(engine_selection=None)

    def snapshot(element_tree):
        elements = _flatten(element_tree)
        css, by_id, frames, hashes, hash_ids = build_element_dict(elements)
        return ScrapedPage(
            elements=elements,
            element_tree_trimmed=element_tree,
            element_tree=element_tree,
            id_to_css_dict=css,
            id_to_element_dict=by_id,
            id_to_frame_dict=frames,
            id_to_element_hash=hashes,
            hash_to_element_ids=hash_ids,
            url="about:blank",
            screenshots=[],
            _document_loader_id="document-before",
            _browser_state=browser_state,
            _clean_up_func=AsyncMock(),
            _scrape_exclude=None,
        )

    scraped_page = snapshot(tree)
    fresh_tree = deepcopy(tree)
    if leading:
        boxes = fresh_tree[0]["children"][0]["children"]
        for index, element in enumerate(boxes):
            if not unchanged:
                element["id"] = f"remounted-box-{index}"
            element["attributes"]["value"] = "123456"[index]
            if plan_kind == "leading-different":
                element["attributes"]["name"] = f"unrelated-pin-{index}"
        if plan_kind == "leading-other-ancestor":
            fresh_tree[0]["attributes"]["id"] = "pin-panel"
        if plan_kind == "leading-other-container":
            fresh_tree[0]["children"][0]["attributes"] = {"id": "pin-form", "aria-label": "PIN entry"}
        if plan_kind == "leading-gone":
            boxes[:] = []
        elif plan_kind == "leading-partial":
            boxes[:] = boxes[:1]
            boxes[0]["id"] = "fixture-box-0"
        if plan_kind == "leading-other-frame":
            for element in _flatten(fresh_tree):
                element["frame"] = "child-frame"
        fresh_tree[1]["id"] = "remounted-submit"
    fresh = snapshot(fresh_tree)

    async def take_snapshot(**kwargs):
        assert leading_ran
        return fresh

    object.__setattr__(scraped_page, "generate_scraped_page_without_screenshots", AsyncMock(side_effect=take_snapshot))
    object.__setattr__(
        scraped_page,
        "refresh",
        AsyncMock(side_effect=AssertionError("The original snapshot must not be refreshed in place")),
    )
    original_snapshot = deepcopy(scraped_page.id_to_element_dict)
    original_css = dict(scraped_page.id_to_css_dict)
    accepted = plan_kind in {"hint", "mixed"} or leading
    value = "123456" if plan_kind in {"pin", "pin-digit"} else _HINT
    # Incoming cache identities were computed before autofill; never replace them with live-value hashes.
    planning_hashes = []
    for index in range(6):
        planned_element = deepcopy(scraped_page.id_to_element_dict[f"fixture-box-{index}"])
        planned_element["attributes"]["value"] = ""
        planning_hashes.append(hash_element(planned_element))
    actions = [
        InputTextAction(
            element_id=f"fixture-box-{index}",
            text=digit,
            skyvern_element_hash=planning_hashes[index],
            skyvern_element_data={
                **scraped_page.id_to_element_dict[f"fixture-box-{index}"],
                "page_url": scraped_page.url,
            },
        )
        for index, digit in enumerate(value)
    ]
    if plan_kind in {"mixed", "contradictory"}:
        actions[0].text = value
    if plan_kind == "contradictory":
        actions[1].text = str((int(value[1]) + 1) % 10)
    if plan_kind.endswith("-digit"):
        actions = actions[:1]
        actions[0].intention = actions[0].response = value
    if leading:
        actions[0].tool_call_id = "otp-first"
        actions.insert(0, ClickAction(element_id="open-authenticator"))
        actions.append(ClickAction(element_id="fixture-submit"))
    else:
        actions.append(ClickAction(element_id="submit"))
    now = datetime.now(UTC)
    organization = make_organization(now)
    organization.max_retries_per_step = 1
    task = make_task(
        now,
        organization,
        task_id=task_id,
        workflow_run_id="workflow",
        navigation_payload={"credential": {"totp": "credential-token"}},
        totp_identifier="test-code",
        navigation_goal="Enter the verification code",
        data_extraction_goal=None,
    )
    step = make_step(now, task, step_id="step", status=StepStatus.created, order=0, output=None)
    task_block = ActionBlock.model_construct(parameters=[], complete_on_download=False)
    current_page = SimpleNamespace(url="about:blank", keyboard=SimpleNamespace(type=AsyncMock()))
    current_page.locator = MagicMock(
        side_effect=lambda css: SimpleNamespace(
            count=AsyncMock(return_value=int(css in fresh.id_to_css_dict.values())),
            bounding_box=AsyncMock(return_value=None),
        )
    )
    browser_state.must_get_working_page = AsyncMock(return_value=current_page)
    delivered = []
    persisted_inputs = []
    submitted = []
    llm_caller = SimpleNamespace(add_tool_result=MagicMock())
    first_ids = [f"fixture-box-{index}" if unchanged else f"remounted-box-{index}" for index in range(6)]

    async def click_handler(action, page, scraped_page, task, step):
        nonlocal leading_ran
        if action.element_id == "open-authenticator":
            leading_ran = True
            return [ActionSuccess()]
        assert action.element_id == "remounted-submit" and scraped_page is fresh
        submitted.append(action.element_id)
        return [ActionSuccess()]

    async def handle_action(*, action, **kwargs):
        if leading:
            if isinstance(action, InputTextAction):
                assert not rejected, "No digit may be dispatched after failed continuity"
                assert kwargs["scraped_page"] is fresh
                assert state.box_element_ids == first_ids and action.element_id == first_ids[0]
                assert action.skyvern_element_data["attributes"]["value"] == "*"
                for attribute in (
                    "hint_code",
                    "valid_from",
                    "valid_until",
                    "filled_code_hash",
                    "filled_at",
                    "credential_placeholders",
                ):
                    assert getattr(state, attribute) == getattr(before_dispatch, attribute)
                persisted_inputs.append(action_for_multi_field_totp_persistence(action))
            else:
                assert kwargs["scraped_page"] is scraped_page
                assert action.element_id in {"open-authenticator", "fixture-submit"}
            return await handler.ActionHandler._handle_action(
                scraped_page=kwargs["scraped_page"],
                page=kwargs["page"],
                task=task,
                step=kwargs["step"],
                action=action,
                allow_stale_refresh=kwargs["allow_stale_refresh"],
            )
        if isinstance(action, InputTextAction):
            persisted_inputs.append(action_for_multi_field_totp_persistence(action))
            if action.totp_timing_info and action.totp_timing_info.get("is_totp_sequence"):
                if code_source == "external":
                    assert await _resolve_multi_field_totp_code(task, state) == _CODE
                return [ActionSuccess(data={"totp_group_filled": True})]
            delivered.append((action.element_id, action.text))
        return [ActionSuccess()]

    create_action = AsyncMock(return_value=SimpleNamespace(action_id="persisted"))
    monkeypatch.setattr(agent_module, "preflight_batch", MagicMock())
    monkeypatch.setattr(agent_module, "get_or_create_wait_config", AsyncMock(return_value={}))
    monkeypatch.setattr(agent_module, "get_wait_time", MagicMock(return_value=0))
    monkeypatch.setattr(agent_module.ActionHandler, "handle_action", handle_action)
    monkeypatch.setattr(agent_module.app.AGENT_FUNCTION, "post_action_execution", AsyncMock())
    monkeypatch.setattr(agent_module.app.DATABASE.workflow_params, "create_action", create_action)
    monkeypatch.setattr(agent_module, "asyncio", ScopedAsyncio(sleep=AsyncMock()))
    if leading:
        elements = _fake_group_elements(6)
        dom = MagicMock()
        dom.get_skyvern_element_by_id = AsyncMock(
            side_effect=lambda element_id, **kwargs: elements[first_ids.index(element_id)]
        )
        monkeypatch.setattr(handler, "DomUtil", lambda *args, **kwargs: dom)
        monkeypatch.setattr(handler, "parse_totp_config", lambda _: _FakeTotp())
        monkeypatch.setattr(handler.time, "time", lambda: 10.0)
        monkeypatch.setattr(
            multi_field_totp,
            "get_main_document_loader_id",
            AsyncMock(return_value="document-after" if plan_kind == "leading-navigation" else "document-before"),
        )
        monkeypatch.setattr(handler, "_apply_secret_visual_mask_if_needed", AsyncMock())
        monkeypatch.setattr(
            handler, "_read_multi_field_totp_values", AsyncMock(side_effect=[list("123456"), list(_CODE)])
        )
        monkeypatch.setattr(handler.app.AGENT_FUNCTION, "wait_for_challenge_solver", AsyncMock())
        monkeypatch.setattr(handler.LLMCallerManager, "get_llm_caller", lambda _: llm_caller)
        monkeypatch.setitem(
            handler.ActionHandler._handled_action_types,
            handler.ActionType.INPUT_TEXT,
            handler._handle_input_text_action,
        )
        monkeypatch.setitem(handler.ActionHandler._handled_action_types, handler.ActionType.CLICK, click_handler)
        remapper = AsyncMock(wraps=handler._refresh_stale_web_action_before_dispatch)
        monkeypatch.setattr(handler, "_refresh_stale_web_action_before_dispatch", remapper)
    agent = ForgeAgent()
    agent.record_artifacts_after_action = AsyncMock(return_value=None)
    agent._persist_scrape_artifacts = AsyncMock()
    agent.async_operation_pool = SimpleNamespace(run_operation=MagicMock())
    retry_scrape = snapshot(deepcopy(fresh_tree))
    agent._scrape_with_type = AsyncMock(side_effect=[scraped_page, retry_scrape])
    agent._build_extract_action_prompt = AsyncMock(
        return_value=agent_module.PromptBuildResult("prompt", False, "extract-action", False)
    )
    agent._generate_step_actions = AsyncMock(side_effect=[(actions, None, False), ([], None, False)])
    finalize = AsyncMock(wraps=agent._finalize_step_execution)
    agent._finalize_step_execution = finalize
    monkeypatch.setattr(agent_module.app.AGENT_FUNCTION, "prepare_step_execution", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_module, "resolve_transient_ui_capture_arm", AsyncMock())
    monkeypatch.setattr(
        agent_module.app.EXPERIMENTATION_PROVIDER, "is_feature_enabled_cached", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(agent_module, "save_step_logs", AsyncMock())
    stored_steps = {step.step_id: step}

    async def update_step_row(*, task_id, step_id, organization_id, **updates):
        stored_steps[step_id] = stored_steps[step_id].model_copy(update=updates)
        return stored_steps[step_id]

    async def create_step_row(*, task_id, organization_id, order, retry_index):
        retry_step = make_step(
            now,
            task,
            step_id="step-retry",
            status=StepStatus.created,
            order=order,
            output=None,
            retry_index=retry_index,
        )
        stored_steps[retry_step.step_id] = retry_step
        return retry_step

    monkeypatch.setattr(agent_module.app.DATABASE.tasks, "update_step", AsyncMock(side_effect=update_step_row))
    monkeypatch.setattr(agent_module.app.DATABASE.tasks, "create_step", AsyncMock(side_effect=create_step_row))

    with skyvern_context.scoped(context):
        if code_source == "external":
            task.navigation_payload["verification_code"] = _CODE
        payload = agent._build_navigation_payload(
            task, expire_verification_code=True, step=step, scraped_page=scraped_page
        )
        assert _CODE not in json.dumps(payload)
        assert state.box_element_ids == [f"fixture-box-{index}" for index in range(6)]
        assert _first_plan_carries_consumable_totp([action.model_dump() for action in actions], True, state) is accepted
        before_dispatch = deepcopy(state)
        step, detailed = await agent.agent_step(
            task=task,
            step=step,
            browser_state=browser_state,
            engine=RunEngine.skyvern_v1,
            organization=organization,
            task_block=task_block,
            complete_verification=False,
        )
        if rejected:
            assert step.status == StepStatus.failed and not step.is_success()
            assert not (isinstance(task_block, ActionBlock) and step.is_success())
            finalize.assert_not_awaited()
            assert detailed.step_exception is None
            assert context.next_step_pre_scraped_data is None
            retry_step = await agent.handle_failed_step(organization, task, step)
            assert retry_step is not None and retry_step.retry_index == 1
            await agent.agent_step(
                task=task,
                step=retry_step,
                browser_state=browser_state,
                engine=RunEngine.skyvern_v1,
                organization=organization,
                task_block=task_block,
                complete_verification=False,
            )
            assert agent._scrape_with_type.await_count == 2
            assert agent._generate_step_actions.await_count == 2
            assert agent._generate_step_actions.await_args.kwargs["scraped_page"] is retry_scrape
            assert agent._build_extract_action_prompt.await_args.args[3] is retry_scrape
            finalize.assert_not_awaited()
        else:
            assert step.status == StepStatus.completed and step.is_success()
            finalize.assert_awaited_once()
    scraped_page.refresh.assert_not_awaited()
    assert scraped_page.id_to_element_dict == original_snapshot
    assert scraped_page.id_to_css_dict == original_css
    assert detailed.actions == actions
    if rejected:
        assert task_id not in context.multi_field_totp and not context.totp_codes
        assert all(action.totp_timing_info is None for action in actions)
        dom.get_skyvern_element_by_id.assert_not_awaited()
        current_page.keyboard.type.assert_not_awaited()
        assert not submitted and not persisted_inputs
        outcome = detailed.action_results[-1]
        assert isinstance(outcome, ActionFailure) and not outcome.success
        assert outcome.exception_type == "MultiFieldTotpGroupGone"
        assert "widget changed after a preceding action" in outcome.exception_message
        assert "re-planned" in outcome.exception_message
        assert step.output.action_results[-1].success is False
        assert step.output.actions_and_results[-1][0].status == ActionStatus.skipped
        assert outcome.skip_remaining_actions and outcome.stop_execution_on_failure
        assert outcome.data == {"totp_group_gone": True, "replan": True}
        assert actions[1].status == ActionStatus.skipped
        llm_caller.add_tool_result.assert_called_once_with(
            {"type": "tool_result", "tool_use_id": "otp-first", "content": STALE_TARGET_TOOL_RESULT}
        )
        assert "Multi-field TOTP plan rejected" in caplog.text
        assert [entry.kwargs["action"].status for entry in create_action.await_args_list] == [ActionStatus.skipped]
        return
    if leading:
        assert scraped_page.generate_scraped_page_without_screenshots.await_count == 2
        assert submitted == ["remounted-submit"]
        assert any(
            entry.args[0] is scraped_page and isinstance(entry.args[2], ClickAction)
            for entry in remapper.await_args_list
        )
    else:
        scraped_page.generate_scraped_page_without_screenshots.assert_not_awaited()
    results = [result.data for _action, results in detailed.actions_and_results for result in results]
    if accepted:
        otp_actions = [action for action in actions if isinstance(action, InputTextAction)]
        assert all(action.totp_timing_info for action in otp_actions)
        otp_results = [{"totp_group_filled": True}, *[{"totp_group_prefilled": True}] * 5]
        assert results == ([None, *otp_results, None] if leading else [*otp_results, None])
        if leading:
            current_page.keyboard.type.assert_awaited_once_with(_CODE)
            assert [action.element_id for action in otp_actions] == first_ids
            assert all(action.totp_timing_info["box_element_ids"] == first_ids for action in otp_actions)
            assert all(
                action.skyvern_element_hash == planning_hashes[index] for index, action in enumerate(otp_actions)
            )
            assert all(action.skyvern_element_data["attributes"]["value"] == "*" for action in otp_actions)
            assert [fresh.id_to_element_dict[element_id]["attributes"]["value"] for element_id in first_ids] == list(
                "123456"
            )
        persisted = [*persisted_inputs, *[entry.kwargs["action"] for entry in create_action.await_args_list]]
        assert [action.text for action in persisted] == ["*"] * 6
        if leading:
            assert all(action.skyvern_element_data["attributes"]["value"] == "*" for action in persisted)
            assert [action.skyvern_element_hash for action in persisted] == planning_hashes
            persisted_json = json.dumps([action.model_dump(mode="json") for action in persisted])
            serialized_output = step.output.model_dump_json()
            serialized_inputs = [
                action
                for action, _ in json.loads(serialized_output)["actions_and_results"]
                if action["action_type"] == "input_text"
            ]
            assert [action["skyvern_element_hash"] for action in serialized_inputs] == planning_hashes
            for element_id in first_ids:
                value_hash = hash_element(fresh.id_to_element_dict[element_id])
                assert value_hash not in planning_hashes
                assert value_hash not in persisted_json
                assert value_hash not in serialized_output
            if plan_kind == "leading-remount":
                # The ordinary stale-target remapper must also preserve armed cache identities.
                stale_action = otp_actions[0].model_copy(deep=True)
                stale_action.element_id = "fixture-box-0"
                rebound = await remapper(scraped_page, current_page, stale_action)
                assert rebound is not None and rebound[0] is fresh
                assert stale_action.element_id == first_ids[0]
                persisted_stale = action_for_multi_field_totp_persistence(stale_action)
                assert persisted_stale.skyvern_element_hash == planning_hashes[0]
                assert hash_element(fresh.id_to_element_dict[first_ids[0]]) not in persisted_stale.model_dump_json()

    else:
        assert all(action.totp_timing_info is None for action in actions)
        assert [action.text for action in persisted_inputs] == [action.text for action in actions[:-1]]
        assert all(result is None for result in results)
        assert delivered == [(action.element_id, action.text) for action in actions[:-1]]
        warnings = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
        assert warnings and all(_HINT not in message and _CODE not in message for message in warnings)


@pytest.mark.parametrize("active_key", ["second", "stale", None])
def test_navigation_payload_selects_only_active_totp_credential(
    monkeypatch: pytest.MonkeyPatch,
    active_key: str | None,
) -> None:
    from skyvern.forge import agent as agent_module

    task_id = "task-active-credential"
    workflow_context = _CredentialContext(_SEED)
    second_seed = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    workflow_context.values["second"] = {"totp": "second-token"}
    workflow_context.secrets["second-token_value"] = second_seed
    monkeypatch.setattr(
        agent_module.app,
        "WORKFLOW_CONTEXT_MANAGER",
        SimpleNamespace(
            workflow_run_contexts={"workflow": workflow_context},
            get_workflow_run_context=lambda _workflow_run_id: workflow_context,
        ),
    )
    task = SimpleNamespace(
        complete_criterion=None,
        terminate_criterion=None,
        task_id=task_id,
        workflow_run_id="workflow",
        navigation_payload={
            "credential": {"totp": "credential-token"},
            "second": {"totp": "second-token"},
        },
        navigation_goal=None,
        data_extraction_goal=None,
    )
    decoys = iter([_HINT, _HINT[::-1]])
    monkeypatch.setattr(agent_module, "_generate_multi_field_totp_hint", lambda digits: next(decoys))
    context = SkyvernContext(task_id=task_id, active_credential_parameter_key="credential")
    with skyvern_context.scoped(context):
        agent = ForgeAgent()
        agent._build_navigation_payload(task, step=SimpleNamespace(), scraped_page=_box_page(6))
        state = context.multi_field_totp[task_id]
        state.valid_from, state.valid_until, state.filled_at = 10.0, 40.0, 12.0
        state.filled_code_hash = hashlib.sha256(_CODE.encode()).hexdigest()
        state.fill_verified = True
        context.totp_codes[f"{task_id}_totp_cache"] = _CODE
        context.active_credential_parameter_key = "credential" if active_key == "stale" else active_key
        if active_key == "stale":
            task.navigation_payload.pop("credential")
        agent._build_navigation_payload(task, step=SimpleNamespace(), scraped_page=_box_page(6))
    if active_key:
        assert context.totp_codes[f"{task_id}_secret"] == second_seed
        assert context.multi_field_totp[task_id].code_source == "secret"
        assert state.hint_code == _HINT[::-1]
        assert state.filled_code_hash is None
        assert state.valid_from is None and state.valid_until is None and state.filled_at is None
        assert not state.fill_verified
        assert f"{task_id}_totp_cache" not in context.totp_codes
    else:
        assert not context.totp_codes
        assert not context.multi_field_totp


@pytest.mark.parametrize("active_key", [None, "missing"])
def test_credential_ambiguity_preserves_external_attempt(
    monkeypatch: pytest.MonkeyPatch, active_key: str | None
) -> None:
    from skyvern.forge import agent as agent_module

    task_id = "task-ambiguous-external"
    workflow_context = _CredentialContext(_SEED)
    workflow_context.values["second"] = {"totp": "second-token"}
    workflow_context.secrets["second-token_value"] = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    monkeypatch.setattr(
        agent_module.app,
        "WORKFLOW_CONTEXT_MANAGER",
        SimpleNamespace(
            workflow_run_contexts={"workflow": workflow_context},
            get_workflow_run_context=lambda _workflow_run_id: workflow_context,
        ),
    )
    task = SimpleNamespace(
        complete_criterion=None,
        terminate_criterion=None,
        task_id=task_id,
        workflow_run_id="workflow",
        navigation_payload=deepcopy(workflow_context.values),
        navigation_goal=None,
        data_extraction_goal=None,
    )
    context = SkyvernContext(
        task_id=task_id,
        active_credential_parameter_key=active_key,
        totp_codes={task_id: _CODE, f"{task_id}_secret": _SEED},
    )
    with skyvern_context.scoped(context):
        agent = ForgeAgent()
        agent._build_navigation_payload(task, step=SimpleNamespace(), scraped_page=_box_page(6))
        state = context.multi_field_totp[task_id]
        state.filled_code_hash = hashlib.sha256(_CODE.encode()).hexdigest()
        state.filled_at = 10.0
        context.pop_totp_code(task_id)
        refreshed = _box_page(6)
        for index, element in enumerate(refreshed.element_tree[0]["children"]):
            element["id"] = f"refreshed-box-{index}"
        agent._build_navigation_payload(task, step=SimpleNamespace(), scraped_page=refreshed)
        assert context.multi_field_totp[task_id] is state
        assert state.box_element_ids == [f"refreshed-box-{index}" for index in range(6)]
        assert state.code_source == "external"
        assert state.filled_code_hash == hashlib.sha256(_CODE.encode()).hexdigest()
        assert state.filled_at == 10.0
        assert context.totp_codes == {f"{task_id}_totp_cache": _CODE}
