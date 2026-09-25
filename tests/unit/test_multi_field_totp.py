"""Focused tests for the page-armed, one-burst multi-field TOTP flow."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest
import structlog
from bs4 import BeautifulSoup
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from skyvern.config import settings
from skyvern.forge import agent as agent_module
from skyvern.forge.agent import (
    ForgeAgent,
    _first_plan_carries_consumable_totp,
    _multi_field_totp_box_group,
)
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import (
    MultiFieldTotpAttempt,
    MultiFieldTotpRejection,
    SkyvernContext,
    action_for_multi_field_totp_persistence,
)
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.services.bitwarden import BitwardenConstants
from skyvern.forge.sdk.workflow import service as workflow_service
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.parameter import CredentialParameter
from skyvern.schemas.run_enums import RunEngine
from skyvern.services import otp_service
from skyvern.services.otp_service import OTPType, OTPValue
from skyvern.utils.action_redaction import redact_action_for_log
from skyvern.webeye.actions import handler
from skyvern.webeye.actions import multi_field_totp as multi_field_totp_module
from skyvern.webeye.actions.actions import (
    ActionStatus,
    ClickAction,
    InputOrSelectContext,
    InputTextAction,
    TerminateAction,
)
from skyvern.webeye.actions.handler import (
    _fill_multi_field_totp_group,
    _resolve_multi_field_totp_code,
)
from skyvern.webeye.actions.multi_field_totp import (
    SECOND_REJECTION_REASON,
    MultiFieldTotpBindingFailure,
    _multi_field_totp_frame_gone,
)
from skyvern.webeye.actions.responses import STALE_TARGET_TOOL_RESULT, ActionFailure, ActionSuccess
from tests.unit.helpers import make_organization, make_step, make_task
from tests.unit.scoped_asyncio import ScopedAsyncio

_SEED = "JBSWY3DPEHPK3PXP"
_HINT = "907182"
_CODE = "650294"
_REAL_SUBMIT_DISCOVERY = multi_field_totp_module.find_multi_field_totp_submit_controls


def _submit_candidate(control):
    return multi_field_totp_module.MultiFieldTotpSubmitControl(
        control,
        multi_field_totp_module._multi_field_totp_submit_control_log_fields(
            {"tag": "button", "type": "submit", "label": "Verify"}, candidate_index=0
        ),
    )


@pytest.fixture(autouse=True)
def stub_fill_observer_for_dom_fakes(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    if request.function.__name__ == "test_multi_field_totp_browser_privacy_after_fill":
        return
    monkeypatch.setattr(
        handler,
        "install_multi_field_totp_fill_observer",
        AsyncMock(return_value=SimpleNamespace(bind=AsyncMock(), refresh=AsyncMock(), stop=MagicMock())),
    )


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
            assert "654321" not in next_prompt.prompt
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
@pytest.mark.parametrize(
    "code_source",
    [
        "secret",
        "external",
        "rejected_equal",
        "registered_only",
        "spent_retry",
        "spent_retry_reused_id",
        "spent_retry_hint",
    ],
)
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
    spent_retry = code_source.startswith("spent_retry")
    state.code_source = "external" if code_source == "rejected_equal" or spent_retry else code_source
    context.multi_field_totp[task_id] = state
    context.totp_codes[f"{task_id}_secret"] = _SEED
    context.totp_codes[f"{task_id}_totp_cache"] = _CODE
    if spent_retry:
        context.multi_field_totp_rejections[task_id] = MultiFieldTotpRejection(
            hashlib.sha256(b"123456").hexdigest(), now, None, "Rejected", retry_used=True, submitted_at=now
        )
    monkeypatch.setattr(handler, "parse_totp_config", lambda _: _FakeTotp())
    monkeypatch.setattr(handler.time, "time", lambda: 40.0)
    target_id = "box-0" if code_source == "spent_retry_reused_id" else "single-otp"
    element = make_input_element_mock(
        element_id=target_id, attrs={"type": "password" if code_source == "rejected_equal" else "text"}
    )
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
    input_text = (
        "AB12C"
        if code_source == "rejected_equal"
        else _CODE
        if code_source in {"registered_only", "spent_retry", "spent_retry_reused_id"}
        else _HINT
    )
    action = InputTextAction(
        element_id=target_id,
        text=input_text,
        task_id=task_id,
        skyvern_element_data={"attributes": {"aria-label": "Password AB12C"}},
    )
    page = _box_page(2 if code_source == "registered_only" else 6)
    if spent_retry:
        page = _box_page(1)
        page.element_tree[0]["children"][0]["id"] = target_id
        page.element_tree[0]["children"][0]["attributes"] = {"type": "text"}
    if code_source == "registered_only":
        page.element_tree[0]["children"][0]["id"] = "single-otp"
        for box in page.element_tree[0]["children"]:
            box["attributes"].pop("maxlength", None)
            box["attributes"]["inputmode"] = "numeric"
    page.id_to_element_dict = {target_id: {"tagName": "input"}}
    with skyvern_context.scoped(context), structlog.testing.capture_logs() as logs:
        if code_source == "rejected_equal":
            assert skyvern_context.normalize_multi_field_totp_code("AB-12C", 6) is None
            context.clear_multi_field_totp_state(task_id)
        if code_source == "registered_only":
            context.clear_multi_field_totp_state(task_id)
            skyvern_context.register_multi_field_totp_candidate(_CODE, for_multi_field=True)
        results = await handler.handle_input_text_action(action, MagicMock(), page, task, step)
        if code_source == "rejected_equal":
            persisted = action_for_multi_field_totp_persistence(action).model_dump_json()
            assert "AB12C" in persisted
            context.clear_multi_field_totp_rejection(task_id)
            assert task_id not in context.multi_field_totp_rejected_candidates
    assert all(result.success for result in results)
    assert written == (
        ["AB12C"] if code_source == "rejected_equal" else [_HINT] if code_source == "spent_retry_hint" else [_CODE]
    )
    assert action.text == input_text
    assert action.totp_timing_info is None
    assert not any(item.get("reason") == "retry_budget_exhausted" for item in logs)
    if code_source != "spent_retry_hint":
        assert any(entry.args[0] is element and entry.kwargs["is_secret_value"] for entry in mask.await_args_list)


def test_persistence_copy_keeps_typed_digit_and_drops_seed_without_mutating_execution_action() -> None:
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
    assert persisted.text == "7"
    assert persisted.reasoning == "use the current code here"
    assert persisted.intention == "enter the code"
    assert persisted.response == "7"
    assert persisted.totp_timing_info == {
        "is_totp_sequence": True,
        "action_index": 0,
        "box_element_ids": ["box-0"],
        "code_source": "external",
    }
    assert _SEED not in str(persisted.totp_timing_info)

    assert persisted.input_or_select_context is not None
    assert persisted.input_or_select_context.intention == "Entered a one-time code digit."
    assert persisted.input_or_select_context.field == "Entered a one-time code digit."
    assert persisted.input_or_select_context.date_format == "Entered a one-time code digit."
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
    monkeypatch.setattr(handler, "get_main_document_loader_id", AsyncMock(return_value="delivery-loader"))

    with skyvern_context.scoped(context):
        initial_code = await _resolve_multi_field_totp_code(task, state)
        result = await _fill_multi_field_totp_group(page, _box_page(6), task, state, initial_code)

    assert state.filled_url == "same"
    assert state.filled_loader_id == "delivery-loader"
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
    monkeypatch: pytest.MonkeyPatch, phase: str, remount_kind: str
) -> None:
    from skyvern.webeye.actions import handler
    from skyvern.webeye.scraper.scraped_page import ScrapedPage
    from skyvern.webeye.scraper.scraper import build_element_dict
    from skyvern.webeye.utils.page import SECRET_VISUAL_MASK_ATTRIBUTE, SECRET_VISUAL_MASK_SCRIPT

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
        step_id="step-binding",
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
    with skyvern_context.scoped(context), structlog.testing.capture_logs() as captured_logs:
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
            assert next_payload["verification_code"] == "*"
    assert all(entry in (call(0.1), call(0.2)) for entry in sleep.await_args_list)
    assert sum(entry.args[0] for entry in sleep.await_args_list if entry == call(0.2)) <= 0.4
    if not accepted:
        rejection_logs = [entry for entry in captured_logs if entry.get("event") == "Multi-field OTP binding rejected"]
        assert len(rejection_logs) == 1
        fields = rejection_logs[0]
        assert fields["task_id"] == task.task_id
        assert fields["workflow_run_id"] == task.workflow_run_id
        assert fields["step_id"] == "step-binding"
        assert fields["reason"] == fields["reason_code"]
        assert code not in str(fields) and _SEED not in str(fields)
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
        assert [action.text for action in persisted] == [action.text for action in otp_actions]
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


@pytest.mark.asyncio
@pytest.mark.parametrize("seam", ["periodic", "planned"])
@pytest.mark.parametrize(
    "scenario",
    [
        "retry",
        "retry_external",
        "retry_external_fullwidth",
        "unsupported_punctuation",
        "unsupported_letter",
        "retry_rebound",
        "retry_remounted_before_barrier",
        "retry_fill_remount",
        "retry_submit_timeout",
        "retry_success_navigation",
        "retry_enables_submit_after_fill",
        "retry_evidence_timeout",
        "retry_evidence_detached",
        "retry_evidence_context",
        "submit_trial_timeout",
        "submission_error",
        "clear_error",
        "poll_error",
        "retry_consumed",
        "reference_rejection",
        "reference_lockout",
        "action_referent",
        "termination_privacy",
        "lockout",
        "server",
        "negated",
        "classifier_error",
        "classifier_timeout",
        "malformed",
        "gone",
        "url",
        "loader",
        "undelivered",
        "no_reasoning",
        "no_attempt",
        "no_group",
        "no_identity",
        "different_identity",
        "used_rebuilt",
        "used_cached_external",
        "used_cached_secret",
        "used_lockout",
        "used_unsubmitted",
        "no_evidence",
        "clear_failed",
    ],
)
async def test_rejection_retry_termination_contract(monkeypatch: pytest.MonkeyPatch, seam: str, scenario: str) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization, task_id="task-rejection", max_steps_per_run=10)
    reasons = {
        "lockout": "The account is locked.",
        "used_lockout": "The account is locked.",
        "server": "Verification failed because the server is unavailable.",
        "negated": "The code was not rejected, but the request failed.",
        "termination_privacy": "The site rejected ABC DEF",
    }
    reason = reasons.get(scenario, "The submitted one-time code was rejected as invalid.")
    if scenario.startswith("reference_"):
        reason = "The visible validation message matches the specified failure condition, so no further authentication actions should be attempted."
        failure_condition = "This passcode is invalid." if scenario == "reference_rejection" else "account locked"
        task.navigation_goal = f'If the page says "{failure_condition}", terminate.'
        task.terminate_criterion = failure_condition
        if scenario == "reference_lockout":
            reasons[scenario] = reason
    if scenario == "action_referent":
        reason = "The visible error message meets the user's stated termination condition."
    terminate = TerminateAction(reasoning=reason)
    if scenario == "action_referent":
        terminate.intention = "What should be done if the verification passcode is reported as invalid?"
        terminate.response = "Terminate the process."
    output = agent_module.AgentStepOutput(
        action_results=[ActionSuccess()] if seam == "planned" else [],
        actions_and_results=[(terminate, [ActionSuccess()])] if seam == "planned" else [],
        errors=[],
    )
    step = make_step(now, task, step_id="step-rejection", status=StepStatus.completed, order=0, output=output)
    next_step = make_step(now, task, step_id="step-next", status=StepStatus.created, order=1, output=None)
    attempt = _attempt()
    attempt.filled_code_hash = hashlib.sha256(_CODE.encode()).hexdigest()
    attempt.filled_at = 10
    attempt.filled_url = task.url
    attempt.filled_loader_id = "loader"
    attempt.valid_from = 0
    if scenario in {
        "retry_external",
        "retry_external_fullwidth",
        "unsupported_punctuation",
        "unsupported_letter",
        "poll_error",
    }:
        attempt.code_source = "external"
        task.totp_identifier = "retry-identifier"
    context = SkyvernContext(task_id=task.task_id, multi_field_totp={task.task_id: attempt})
    context.totp_codes[f"{task.task_id}_totp_cache"] = _CODE
    if scenario.startswith("used_"):
        context.multi_field_totp_rejections[task.task_id] = MultiFieldTotpRejection(
            attempt.filled_code_hash,
            now,
            0,
            reason,
            retry_used=True,
            submitted_at=None if scenario == "used_unsubmitted" else now,
        )
        if scenario.startswith("used_cached_"):
            attempt.code_source = scenario.removeprefix("used_cached_")
            context.totp_codes[f"{task.task_id}_totp_cache"] = "483917"
            attempt.filled_code_hash = hashlib.sha256(b"483917").hexdigest()
            context.multi_field_totp_rejections[task.task_id].delivered_code_hashes.add(attempt.filled_code_hash)
        else:
            context.clear_multi_field_totp_state(task.task_id)
            context.multi_field_totp[task.task_id] = _attempt()
    original_ledger = dict(context.multi_field_totp_rejections)
    if scenario == "undelivered":
        attempt.filled_at = None
    elif scenario == "no_attempt":
        context.multi_field_totp.pop(task.task_id)
    elif scenario == "no_reasoning":
        reason = ""
        terminate.reasoning = ""
    page = SimpleNamespace(url=task.url if scenario != "url" else "https://example.com/elsewhere")
    scraped = SimpleNamespace(
        url=task.url,
        box_element_ids=attempt.box_element_ids.copy(),
        id_to_frame_dict={key: "main.frame" for key in attempt.box_element_ids},
        id_to_css_dict={key: f"#{key}" for key in attempt.box_element_ids},
    )
    scraped.generate_scraped_page_without_screenshots = AsyncMock(return_value=scraped)
    monkeypatch.setattr(agent_module, "_multi_field_totp_box_group", lambda snapshot, _digits: snapshot.box_element_ids)
    if scenario == "no_group":
        scraped.box_element_ids = []
    if scenario == "different_identity":
        attempt.filled_group_identity = "original-group"
    monkeypatch.setattr(
        agent_module,
        "multi_field_totp_group_identity",
        lambda *_: None if scenario == "no_identity" else "delivered-group",
    )
    browser_state = SimpleNamespace(engine_selection=None)
    agent = ForgeAgent()
    monkeypatch.setattr(
        agent_module,
        "get_main_document_loader_id",
        AsyncMock(return_value="other" if scenario == "loader" else "loader"),
    )
    monkeypatch.setattr(
        agent_module,
        "_refresh_multi_field_totp_group_binding",
        AsyncMock(
            return_value=MultiFieldTotpBindingFailure.TEARDOWN
            if scenario == "gone"
            else (scraped, attempt.box_element_ids)
        ),
    )
    if scenario in {"retry_rebound", "retry_fill_remount", "retry_remounted_before_barrier"}:
        rebound_ids = [f"fresh-{index}" for index in range(6)]
        rebound = SimpleNamespace(
            url=task.url,
            box_element_ids=rebound_ids,
            id_to_frame_dict={key: "main.frame" for key in rebound_ids},
            id_to_css_dict={key: f"#{key}" for key in rebound_ids},
        )
        rebound.generate_scraped_page_without_screenshots = AsyncMock(return_value=rebound)
        if scenario == "retry_remounted_before_barrier":
            scraped.generate_scraped_page_without_screenshots.return_value = rebound

        async def rebind(snapshot, _page, state, **kwargs):
            if snapshot.box_element_ids != state.box_element_ids:
                return MultiFieldTotpBindingFailure.UNCONFIRMED
            if scenario == "retry_fill_remount" and state.box_element_ids == scraped.box_element_ids:
                return (rebound, rebound.box_element_ids) if fill.await_args else (scraped, scraped.box_element_ids)
            return rebound, rebound.box_element_ids

        monkeypatch.setattr(agent_module, "_refresh_multi_field_totp_group_binding", rebind)
    classifier = AsyncMock(return_value={"code_rejected": scenario not in reasons, "reason": "Classification"})
    if scenario.startswith("reference_"):

        async def classify_reference(**kwargs):
            assert task.navigation_goal in kwargs["prompt"]
            assert task.terminate_criterion in kwargs["prompt"]
            assert reason in kwargs["prompt"]
            return {"code_rejected": scenario == "reference_rejection", "reason": "Referenced condition"}

        classifier.side_effect = classify_reference
    if scenario == "classifier_error":
        classifier.side_effect = RuntimeError("Classifier unavailable")
    if scenario == "classifier_timeout":
        classifier.side_effect = TimeoutError
    if scenario == "malformed":
        classifier.return_value = {"code_rejected": "true", "reason": "Ambiguous"}
    monkeypatch.setattr(agent_module, "get_org_aware_primary_llm_api_handler", lambda: classifier)
    monkeypatch.setattr(
        agent_module.LLMAPIHandlerFactory, "get_override_llm_api_handler", lambda *args, **kwargs: classifier
    )
    fresh_code = "483917"
    resolve_external = AsyncMock(return_value=SimpleNamespace(value=fresh_code, from_credential_seed=False))
    supplied_external = {
        "retry_external_fullwidth": "４８３９１７",
        "unsupported_punctuation": "AB-12C",
        "unsupported_letter": "ABé12C",
    }.get(scenario, fresh_code)
    resolve_external.return_value.value = supplied_external
    if scenario == "poll_error":
        resolve_external.side_effect = RuntimeError(f"Poll error containing {_CODE}")
    monkeypatch.setattr(agent_module, "poll_otp_value", resolve_external)
    credential_resolution = AsyncMock(return_value=SimpleNamespace(value="123456", from_credential_seed=True))
    monkeypatch.setattr(agent_module, "resolve_otp_value", credential_resolution)
    monkeypatch.setattr(agent_module, "_resolve_multi_field_totp_code", AsyncMock(return_value=fresh_code))
    clear = AsyncMock(return_value=scenario != "clear_failed")
    if scenario == "clear_error":
        clear.side_effect = TimeoutError(f"Clear error containing {_CODE}")
    fill = AsyncMock(return_value=ActionSuccess())
    if scenario == "retry_consumed":
        fill.return_value = ActionSuccess(data={"totp_submission_observed": True})
    submit = AsyncMock(return_value=scenario != "no_evidence")
    submission_errors = []
    if scenario == "retry_fill_remount":

        async def fill_remounted(*args):
            attempt.box_element_ids = rebound_ids
            return ActionSuccess()

        async def submit_remounted(_page, snapshot, state, **kwargs):
            try:
                await multi_field_totp_module._retry_box_locators(_page, snapshot, state.box_element_ids)
            except KeyError as exc:
                submission_errors.append(exc)
                raise
            assert snapshot is rebound
            return True

        fill.side_effect = fill_remounted
        submit.side_effect = submit_remounted
        monkeypatch.setattr(
            multi_field_totp_module, "resolve_locator", AsyncMock(return_value=(SimpleNamespace(), None))
        )
    if scenario in {
        "retry_submit_timeout",
        "retry_success_navigation",
        "retry_enables_submit_after_fill",
        "retry_evidence_timeout",
        "retry_evidence_detached",
        "retry_evidence_context",
        "submit_trial_timeout",
        "submission_error",
    }:
        control = SimpleNamespace(
            count=AsyncMock(return_value=1),
            get_attribute=AsyncMock(return_value=None),
            is_visible=AsyncMock(return_value=True),
            is_enabled=AsyncMock(return_value=True),
        )
        clicked = False

        async def click_submit(**kwargs):
            nonlocal clicked
            if kwargs.get("trial"):
                if scenario == "submit_trial_timeout":
                    raise PlaywrightTimeoutError("Control is covered")
                return
            assert kwargs["no_wait_after"]
            clicked = True
            if scenario in {"retry_submit_timeout", "retry_success_navigation", "retry_enables_submit_after_fill"}:
                page.url = "https://example.com/accepted"
            if scenario == "retry_submit_timeout":
                raise PlaywrightTimeoutError("Navigation timed out after dispatch")

        async def probe_widget(*args, **kwargs):
            if not clicked:
                return {"root": "before"}
            if scenario == "retry_evidence_timeout":
                page.url = "https://example.com/accepted"
                raise PlaywrightTimeoutError("Widget disappeared during navigation")
            if scenario == "retry_evidence_detached":
                box.count.return_value = 0
                raise PlaywrightError("Element is not attached to the DOM")
            if scenario == "retry_evidence_context":
                box.count.return_value = 0
                raise PlaywrightError("Execution context was destroyed, most likely because of a navigation")
            if scenario == "submission_error":
                raise TypeError(f"Unexpected probe error containing {_CODE}")
            raise AssertionError("Navigation must be checked before the widget probe")

        control.click = click_submit
        box = SimpleNamespace(
            count=AsyncMock(return_value=1), input_value=AsyncMock(return_value="*"), press=AsyncMock()
        )
        monkeypatch.setattr(multi_field_totp_module, "_retry_box_locators", AsyncMock(return_value=[box]))
        monkeypatch.setattr(multi_field_totp_module, "get_main_document_loader_id", AsyncMock(return_value="loader"))
        monkeypatch.setattr(
            multi_field_totp_module,
            "_refresh_multi_field_totp_group_binding",
            AsyncMock(return_value=(scraped, attempt.box_element_ids)),
        )
        monkeypatch.setattr(
            multi_field_totp_module,
            "find_multi_field_totp_submit_controls",
            AsyncMock(return_value=[_submit_candidate(control)]),
        )
        monkeypatch.setattr(
            multi_field_totp_module,
            "_multi_field_totp_widget_scopes",
            AsyncMock(return_value=[SimpleNamespace(evaluate=probe_widget)]),
        )
        if scenario == "retry_enables_submit_after_fill":
            filled = False

            async def enable_on_fill(*args):
                nonlocal filled
                filled = True
                return ActionSuccess()

            async def attribute(name, **kwargs):
                if name == "disabled":
                    return None if filled else ""
                return {"type": "submit", "aria-label": "Verify"}.get(name)

            fill.side_effect = enable_on_fill
            control.get_attribute = attribute
            control.is_enabled = AsyncMock(side_effect=lambda **_: filled)
            form = SimpleNamespace(
                count=AsyncMock(return_value=1), get_attribute=AsyncMock(return_value="otp"), evaluate=probe_widget
            )
            form.filter = lambda **_: form
            form.locator = lambda _: SimpleNamespace(nth=lambda _: control)

            async def form_evaluate(script, *args, **kwargs):
                if script == multi_field_totp_module._MULTI_FIELD_TOTP_SUBMIT_METADATA_JS:
                    return [
                        {
                            "tag": "button",
                            "type": "submit",
                            "label": "Verify",
                            "has_form": True,
                            "form_matches": True,
                            "disabled": not filled,
                            "aria_disabled": False,
                            "visible": True,
                        }
                    ]
                return await probe_widget(script, *args, **kwargs)

            form.evaluate = form_evaluate
            box.locator = control.locator = lambda _: form
            monkeypatch.setattr(
                multi_field_totp_module, "_multi_field_totp_widget_scopes", AsyncMock(return_value=[form])
            )
            monkeypatch.setattr(
                multi_field_totp_module, "find_multi_field_totp_submit_controls", _REAL_SUBMIT_DISCOVERY
            )
        monkeypatch.setattr(multi_field_totp_module, "asyncio", ScopedAsyncio(sleep=AsyncMock()))
        submit = multi_field_totp_module.submit_multi_field_totp_retry
    monkeypatch.setattr(agent_module, "clear_multi_field_totp_boxes", clear)
    monkeypatch.setattr(agent_module, "_fill_multi_field_totp_group", fill)
    monkeypatch.setattr(agent_module, "submit_multi_field_totp_retry", submit)
    monkeypatch.setattr(agent_module.app.DATABASE.tasks, "create_step", AsyncMock(return_value=next_step))
    monkeypatch.setattr(agent, "update_step", AsyncMock(return_value=step))
    update_task = AsyncMock(return_value=task)
    monkeypatch.setattr(agent, "update_task", update_task)
    monkeypatch.setattr(agent, "get_failure_reason_for_task", AsyncMock(return_value=reason))
    budget = AsyncMock(return_value=None)
    monkeypatch.setattr(agent, "_check_workflow_run_step_budget", budget)
    monkeypatch.setattr(agent, "check_user_goal_complete", AsyncMock(return_value=terminate))
    monkeypatch.setattr(agent, "_speculate_next_step_plan", AsyncMock(return_value=SimpleNamespace(llm_metadata=None)))
    if seam == "periodic" and scenario == "retry_rebound":
        speculation_started = asyncio.Event()
        speculation_ended = asyncio.Event()

        async def speculate(**kwargs):
            speculation_started.set()
            try:
                await asyncio.Future()
            finally:
                attempt.box_element_ids = rebound.box_element_ids
                scraped.generate_scraped_page_without_screenshots.return_value = rebound
                speculation_ended.set()

        async def classify(**kwargs):
            await speculation_started.wait()
            return {"code_rejected": True, "reason": "Rejected"}

        async def resolve_after_cancellation(*args):
            assert speculation_ended.is_set()
            return fresh_code

        classifier.side_effect = classify
        monkeypatch.setattr(agent, "_speculate_next_step_plan", speculate)
        monkeypatch.setattr(agent_module, "_resolve_multi_field_totp_code", resolve_after_cancellation)

    monkeypatch.setattr(agent, "_persist_speculative_metadata_for_discarded_plan", AsyncMock())
    monkeypatch.setattr(agent, "record_artifacts_after_action", AsyncMock())
    monkeypatch.setattr(agent_module.ActionHandler, "handle_action", AsyncMock(return_value=[ActionSuccess()]))
    if scenario.startswith("used_cached_"):
        with skyvern_context.scoped(context):
            terminal_plan = {"place_to_enter_verification_code": True, "actions": [terminate.model_dump(mode="json")]}
            assert (
                await agent.handle_potential_verification_code(task, step, scraped, browser_state, terminal_plan)
                == terminal_plan
            )
    with skyvern_context.scoped(context), structlog.testing.capture_logs() as retry_logs:
        if scenario == "termination_privacy":
            skyvern_context.register_multi_field_totp_candidate("ABCDEF", task_id=task.task_id, for_multi_field=True)
            if seam == "planned":
                assert "ABC DEF" in json.dumps(step.output.model_dump())
            assert terminate.reasoning == reason
        if seam == "periodic":
            result = await agent._handle_completed_step_with_parallel_verification(
                organization,
                task,
                step,
                page,
                browser_state,
                scraped,
                RunEngine.skyvern_v1,
            )
        else:
            result = await agent.handle_completed_step(
                organization,
                task,
                step,
                page,
                browser_state=browser_state,
                scraped_page=scraped,
                complete_verification=False,
            )
        for pending in context.pending_speculative_persist_tasks:
            await pending
    assert not submission_errors, repr(submission_errors)
    if scenario in {
        "retry",
        "retry_external",
        "retry_external_fullwidth",
        "retry_rebound",
        "retry_fill_remount",
        "retry_submit_timeout",
        "retry_success_navigation",
        "retry_enables_submit_after_fill",
        "retry_evidence_timeout",
        "retry_evidence_detached",
        "retry_evidence_context",
        "retry_remounted_before_barrier",
        "retry_consumed",
        "reference_rejection",
        "action_referent",
    }:
        assert result[2] is next_step
        assert update_task.await_args is None
        assert budget.await_args is not None
        assert context.multi_field_totp_rejections[task.task_id].submitted_at is not None
        assert context.multi_field_totp_rejections[task.task_id].retry_used
        assert (
            hashlib.sha256(fresh_code.encode()).hexdigest()
            in context.multi_field_totp_rejections[task.task_id].delivered_code_hashes
        )
        assert fresh_code in context.runtime_secret_values
        assert context.speculative_plans == {}
        if scenario == "retry_consumed":
            assert submit.await_args is None
        if scenario in {"retry_external", "retry_external_fullwidth"}:
            assert credential_resolution.await_args is None
            assert context.totp_codes[f"{task.task_id}_totp_cache"] == fresh_code
            rejection = context.multi_field_totp_rejections[task.task_id]
            assert resolve_external.await_args.kwargs["created_after"] == datetime.fromtimestamp(10, UTC).replace(
                tzinfo=None
            )
            assert resolve_external.await_args.kwargs["rejected_code_hash"] == rejection.rejected_code_hash
            assert resolve_external.await_args.kwargs["multi_field_expected_digits"] == attempt.expected_digits
            with skyvern_context.scoped(context):
                prompt_fields = agent_module._replace_multi_field_totp_prompt_code(
                    task.task_id, {"navigation_goal": f"Use code {_CODE}; terminate if rejected."}
                )
            assert _CODE not in prompt_fields["navigation_goal"]
    else:
        assert result[2] is None
        skipped = [entry for entry in retry_logs if entry["event"] == "Multi-field TOTP retry skipped"]
        if scenario in {"used_rebuilt", "used_cached_external", "used_cached_secret"}:
            assert not skipped
        else:
            assert len(skipped) == 1
            assert skipped[0]["reason"] == {
                "no_reasoning": "no_reasoning",
                "no_attempt": "no_attempt",
                "undelivered": "no_delivery",
                "url": "url_changed",
                "loader": "loader_changed",
                "gone": "binding_teardown",
                "no_group": "group_not_found",
                "no_identity": "group_identity_unavailable",
                "different_identity": "group_identity_changed",
                "used_unsubmitted": "retry_budget_exhausted",
                "classifier_error": "classifier_error",
                "classifier_timeout": "classifier_error",
            }.get(scenario, skipped[0]["reason"])
            assert _CODE not in str(skipped) and fresh_code not in str(skipped)
        assert update_task.await_args.kwargs["failure_reason"] == (
            SECOND_REJECTION_REASON
            if scenario in {"used_rebuilt", "used_cached_external", "used_cached_secret"}
            else reason
        )
        assert context.multi_field_totp_rejections == original_ledger
        if scenario.startswith("unsupported_"):
            assert f"{task.task_id}_totp_cache" not in context.totp_codes
            assert any(entry.get("reason") == "unsupported_code_format" for entry in retry_logs)
            assert supplied_external not in str(retry_logs)
        if scenario not in {"no_evidence", "clear_failed", "clear_error", "submission_error", "submit_trial_timeout"}:
            assert clear.await_args is None
            assert fill.await_args is None
        expected_error = {
            "submission_error": ("submission_failed", "TypeError"),
            "clear_error": ("clear_failed", "TimeoutError"),
            "poll_error": ("external_code_poll_failed", "RuntimeError"),
            "submit_trial_timeout": ("submit_control_not_actionable", "TimeoutError"),
        }.get(scenario)
        if expected_error:
            failure = next(entry for entry in retry_logs if entry["event"] == "Multi-field TOTP unable to resubmit")
            assert (failure["reason"], failure["error_type"]) == expected_error
        if scenario == "submit_trial_timeout":
            assert not clicked
    if scenario == "termination_privacy":
        assert "ABC DEF" not in str(retry_logs)
    if classifier.await_args:
        prompt = classifier.await_args.kwargs["prompt"]
        if scenario == "action_referent":
            assert terminate.intention in prompt and terminate.response in prompt
        assert _CODE not in prompt and fresh_code not in prompt
        assert classifier.await_args.kwargs["screenshots"] == []
        decision = next(entry for entry in retry_logs if entry["event"] == "Multi-field TOTP rejection check")
        assert decision["code_rejected"] == (
            scenario not in reasons and scenario not in {"classifier_error", "classifier_timeout", "malformed"}
        )
        assert decision["source"] == seam
    for entry in retry_logs:
        if entry["event"] in {
            "Multi-field TOTP rejection detected, retrying once",
            "Multi-field TOTP retry submitted",
            "Multi-field TOTP unable to resubmit",
            "Multi-field TOTP rejected twice",
            "Multi-field TOTP rejection check",
            "Multi-field TOTP retry skipped",
            "Multi-field TOTP submit dispatch",
        }:
            assert entry["task_id"] == task.task_id
            assert entry["workflow_run_id"] == task.workflow_run_id
            assert entry["step_id"] == step.step_id
            assert _CODE not in str(entry) and fresh_code not in str(entry)
            assert hashlib.sha256(_CODE.encode()).hexdigest() not in str(entry)


@pytest.mark.asyncio
@pytest.mark.parametrize("collisions", [0, 1, 2, 3])
async def test_retry_secret_advances_window_and_excludes_rejected_hash(
    monkeypatch: pytest.MonkeyPatch, collisions: int
) -> None:
    task = _fill_task()
    state = _attempt()
    context = SkyvernContext(task_id=task.task_id, totp_codes={f"{task.task_id}_secret": _SEED})
    context.multi_field_totp_rejections[task.task_id] = MultiFieldTotpRejection(
        hashlib.sha256(_CODE.encode()).hexdigest(),
        datetime.now(UTC),
        0,
        "Code rejected",
    )
    totp = _FakeTotp()

    def generate(timestamp: int) -> str:
        totp.at_values.append(timestamp)
        return _CODE if timestamp <= collisions * 30 else "483917"

    totp.at = generate

    async def wait_window(*, now: float, next_window_from: float, interval: int) -> tuple[float, float]:
        return next_window_from, next_window_from + interval

    monkeypatch.setattr(handler, "parse_totp_config", lambda _: totp)
    monkeypatch.setattr(handler.time, "time", lambda: 1)
    monkeypatch.setattr(handler, "_wait_for_next_multi_field_totp_window", wait_window)
    with skyvern_context.scoped(context):
        code = await _resolve_multi_field_totp_code(task, state)
    if collisions == 3:
        assert isinstance(code, ActionFailure)
        assert f"{task.task_id}_totp_cache" not in context.totp_codes
    else:
        assert code == "483917"
        assert state.valid_from is not None and state.valid_from > 0
        assert (
            hashlib.sha256(code.encode()).hexdigest()
            != context.multi_field_totp_rejections[task.task_id].rejected_code_hash
        )


@pytest.mark.parametrize("transient", [_CODE, "483917", None])
def test_retry_payload_cannot_recache_rejected_inline_code(
    monkeypatch: pytest.MonkeyPatch, transient: str | None
) -> None:
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), navigation_payload={"verification_code": _CODE})
    step = make_step(now, task, step_id="step", status=StepStatus.created, order=0, output=None)
    state = _attempt()
    state.code_source = "external"
    state.hint_code = _HINT
    context = SkyvernContext(
        task_id=task.task_id,
        multi_field_totp={task.task_id: state},
        totp_codes={task.task_id: transient} if transient else {},
    )
    context.multi_field_totp_rejections[task.task_id] = MultiFieldTotpRejection(
        hashlib.sha256(_CODE.encode()).hexdigest(),
        now,
        None,
        "Rejected",
    )
    monkeypatch.setattr(agent_module, "_multi_field_totp_box_group", lambda *args: state.box_element_ids)
    with skyvern_context.scoped(context):
        payload = ForgeAgent()._build_navigation_payload(task, step=step, scraped_page=SimpleNamespace(elements=[]))
    assert payload == {"verification_code": _HINT}
    assert context.totp_codes.get(f"{task.task_id}_totp_cache") == (transient if transient != _CODE else None)
    assert context.totp_codes.get(task.task_id) != _CODE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "signal", ["none", "url", "loader", "cleared", "widget", "unrelated", "teardown", "unconfirmed"]
)
async def test_retry_submission_requires_page_evidence(monkeypatch: pytest.MonkeyPatch, signal: str) -> None:
    attempt = _attempt()
    attempt.observed_max_filled = attempt.expected_digits
    page = SimpleNamespace(
        url="other" if signal == "url" else "same",
        evaluate=AsyncMock(return_value="after" if signal in {"widget", "unrelated"} else "before"),
    )
    scraped = SimpleNamespace(url="same", _document_loader_id="loader")
    box = SimpleNamespace(
        count=AsyncMock(return_value=0 if signal == "teardown" else 2 if signal == "unconfirmed" else 1),
        input_value=AsyncMock(return_value="" if signal == "cleared" else "*"),
    )
    monkeypatch.setattr(multi_field_totp_module, "_retry_box_locators", AsyncMock(return_value=[box]))
    monkeypatch.setattr(
        multi_field_totp_module,
        "get_main_document_loader_id",
        AsyncMock(return_value="other" if signal == "loader" else "loader"),
    )
    monkeypatch.setattr(
        multi_field_totp_module,
        "_refresh_multi_field_totp_group_binding",
        AsyncMock(
            return_value=(
                multi_field_totp_module.MultiFieldTotpBindingFailure.TEARDOWN
                if signal == "teardown"
                else multi_field_totp_module.MultiFieldTotpBindingFailure.UNCONFIRMED
            )
        ),
    )
    widget = SimpleNamespace(evaluate=AsyncMock(return_value={"root": "after" if signal == "widget" else "before"}))
    monkeypatch.setattr(multi_field_totp_module, "_multi_field_totp_widget_scopes", AsyncMock(return_value=[widget]))
    result = await multi_field_totp_module.multi_field_totp_submission_evidence(
        page,
        scraped,
        attempt,
        baseline=multi_field_totp_module.MultiFieldTotpSubmissionBaseline(
            "same", "loader", {"root": "before"}, frozenset()
        ),
        post_dispatch=True,
    )
    assert result == (signal not in {"none", "unconfirmed", "unrelated"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["auto", "button", "enter", "no_evidence", "click_timeout", "enter_timeout", "detached"]
)
async def test_retry_submit_only_claims_evidence_and_does_not_resubmit_auto(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    state = _attempt()
    scraped = SimpleNamespace(url="same")
    page = SimpleNamespace(url="same", evaluate=AsyncMock(return_value="baseline"))
    control = SimpleNamespace(
        click=AsyncMock(),
        count=AsyncMock(return_value=0 if mode == "detached" else 1),
        get_attribute=AsyncMock(return_value=None),
        is_visible=AsyncMock(return_value=True),
        is_enabled=AsyncMock(return_value=True),
    )
    box = SimpleNamespace(press=AsyncMock(), count=AsyncMock(return_value=1))
    submitted = False

    async def mark_submitted(*args, **kwargs):
        nonlocal submitted
        if kwargs.get("trial"):
            return
        if args:
            assert args == ("Enter",) and "no_wait_after" not in kwargs
        else:
            assert kwargs["no_wait_after"]
        submitted = True
        if mode.endswith("timeout"):
            page.url = "accepted"
            error = TimeoutError if mode == "click_timeout" else PlaywrightTimeoutError
            raise error("Navigation timed out after dispatch")

    control.click.side_effect = mark_submitted
    box.press.side_effect = mark_submitted

    original_evidence = multi_field_totp_module.multi_field_totp_submission_evidence

    async def evidence(*args, **kwargs):
        if submitted and mode.endswith("timeout"):
            return await original_evidence(*args, **kwargs)
        return mode == "auto" or (mode != "no_evidence" and submitted)

    monkeypatch.setattr(multi_field_totp_module, "multi_field_totp_submission_evidence", evidence)
    monkeypatch.setattr(
        multi_field_totp_module,
        "_refresh_multi_field_totp_group_binding",
        AsyncMock(return_value=(scraped, state.box_element_ids)),
    )
    monkeypatch.setattr(multi_field_totp_module, "_retry_box_locators", AsyncMock(return_value=[box]))
    monkeypatch.setattr(
        multi_field_totp_module,
        "find_multi_field_totp_submit_controls",
        AsyncMock(return_value=[] if mode in {"enter", "enter_timeout"} else [_submit_candidate(control)]),
    )
    monkeypatch.setattr(
        multi_field_totp_module,
        "_multi_field_totp_widget_scopes",
        AsyncMock(return_value=[SimpleNamespace(evaluate=AsyncMock(return_value="baseline"))]),
        raising=False,
    )
    monkeypatch.setattr(multi_field_totp_module, "asyncio", ScopedAsyncio(sleep=AsyncMock()))
    result = await multi_field_totp_module.submit_multi_field_totp_retry(page, scraped, state)
    assert result == (mode != "no_evidence")
    assert submitted == (mode != "auto")
    assert (box.press.await_args is not None) == (mode in {"enter", "enter_timeout", "detached"})


@pytest.mark.asyncio
@pytest.mark.parametrize("corroboration", ["none", "url", "loader", "boxes_gone"])
@pytest.mark.parametrize("probe_result", ["timeout", "detached", "context_lost"])
async def test_widget_probe_teardown_requires_corroboration(
    monkeypatch: pytest.MonkeyPatch, corroboration: str, probe_result: str
) -> None:
    page = SimpleNamespace(url="same")
    scraped = SimpleNamespace(url="same", _document_loader_id="loader")
    box = SimpleNamespace(count=AsyncMock(return_value=1), input_value=AsyncMock(return_value="*"))
    loader = AsyncMock(return_value="loader")
    errors = {
        "timeout": PlaywrightTimeoutError("Widget read timed out"),
        "detached": PlaywrightError("Element is not attached to the DOM"),
        "context_lost": PlaywrightError("Cannot find context with specified id"),
    }

    async def evaluate(*args, **kwargs):
        if corroboration == "url":
            page.url = "accepted"
        elif corroboration == "loader":
            loader.return_value = "new-loader"
        elif corroboration == "boxes_gone":
            box.count.return_value = 0
        raise errors[probe_result]

    monkeypatch.setattr(multi_field_totp_module, "get_main_document_loader_id", loader)
    monkeypatch.setattr(multi_field_totp_module, "_retry_box_locators", AsyncMock(return_value=[box]))
    monkeypatch.setattr(
        multi_field_totp_module,
        "_multi_field_totp_widget_scopes",
        AsyncMock(return_value=[SimpleNamespace(evaluate=evaluate)]),
    )
    baseline = multi_field_totp_module.MultiFieldTotpSubmissionBaseline(
        "same",
        "loader",
        {"root": "before"},
        frozenset(),
    )
    if corroboration == "none":
        with pytest.raises(type(errors[probe_result])):
            await multi_field_totp_module.multi_field_totp_submission_evidence(
                page,
                scraped,
                _attempt(),
                baseline=baseline,
                post_dispatch=True,
            )
    else:
        assert await multi_field_totp_module.multi_field_totp_submission_evidence(
            page,
            scraped,
            _attempt(),
            baseline=baseline,
            post_dispatch=True,
        )


@pytest.mark.parametrize("digits", [4, 6, 8])
@pytest.mark.parametrize("alphabet", ["65029483", "QWERTYUI", "Q7e2R8u1"])
@pytest.mark.parametrize("source", ["external", "secret"])
@pytest.mark.parametrize("torn_down", [False, True])
def test_retry_masks_rejected_prompt_fields_after_cache_replacement(
    digits: int, alphabet: str, source: str, torn_down: bool
) -> None:
    rejected_code = alphabet[:digits]
    fresh_code = "48391726"[:digits]
    hint = "90718235"[:digits]
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), navigation_payload={"note": f"Code: {rejected_code}."})
    state = MultiFieldTotpAttempt([f"box-{i}" for i in range(digits)], digits, source, hint_code=hint)
    context = SkyvernContext(
        multi_field_totp={task.task_id: state}, totp_codes={f"{task.task_id}_totp_cache": fresh_code}
    )
    rejection = MultiFieldTotpRejection(
        hashlib.sha256(rejected_code.encode()).hexdigest(),
        now,
        None,
        "Rejected",
        expected_digits=digits,
        hint_code=hint,
    )
    context.multi_field_totp_rejections[task.task_id] = rejection
    fields = {
        name: f"Rejected ({rejected_code}); fresh ({fresh_code})."
        for name in (
            "navigation_goal",
            "navigation_payload",
            "data_extraction_goal",
            "complete_criterion",
            "terminate_criterion",
            "custom_guidance",
        )
    }
    fields["nested"] = {"items": [f"Code={rejected_code}", f"id{rejected_code}x", f"9{rejected_code}9"]}
    original = deepcopy(fields)
    if torn_down:
        context.clear_multi_field_totp_state(task.task_id)
    with skyvern_context.scoped(context):
        substituted = agent_module._replace_multi_field_totp_prompt_code(task.task_id, fields)
        payload = ForgeAgent()._build_navigation_payload(task)
    assert fields == original
    for name in fields.keys() - {"nested"}:
        assert rejected_code not in substituted[name]
        if source == "external" and not torn_down:
            assert fresh_code not in substituted[name]
    assert substituted["nested"]["items"] == [f"Code={hint}", f"id{rejected_code}x", f"9{rejected_code}9"]
    assert rejected_code not in json.dumps(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("markup", "selected"),
    [
        ("<form>{boxes}</form>{newsletter}", None),
        ('<section>{boxes}<button id="own">Verify</button></section>', "own"),
        ('<form>{boxes}<button id="own" type="submit">Verify</button></form>{newsletter}', "own"),
        ('<form>{boxes}<button id="own" type="submit">Send</button><button>Verify</button></form>', "own"),
        ('<form>{boxes}<button form="newsletter" type="submit">Verify</button></form>{newsletter}', None),
        ("<section><div>{boxes}</div>{newsletter}</section>", None),
        ('<section><div>{boxes}</div>{newsletter}<button id="own">Continue</button></section>', "own"),
        ("<section><div><div><div>{boxes}</div></div></div><button>Verify</button></section>", None),
        ('<div>{boxes}</div><button type="submit">Verify</button>{newsletter}', None),
    ],
)
async def test_retry_submit_discovery_stays_with_otp_widget(markup: str, selected: str | None) -> None:
    class DomLocator:
        def __init__(self, nodes):
            self.nodes = nodes

        async def count(self):
            return len(self.nodes)

        def nth(self, index):
            return DomLocator(self.nodes[index : index + 1])

        def filter(self, *, has):
            return DomLocator(
                [
                    node
                    for node in self.nodes
                    if any(any(parent is node for parent in child.parents) for child in has.nodes)
                ]
            )

        def locator(self, selector):
            nodes = []
            for node in self.nodes:
                if selector == "xpath=ancestor::form[1]":
                    parent = node.find_parent("form")
                    candidates = [parent] if parent else []
                elif selector.startswith("xpath=ancestor::*"):
                    candidates = list(reversed(list(node.parents)))
                    candidates = [parent for parent in candidates if parent.name != "[document]"]
                    if "not(self::body or self::html)" in selector:
                        candidates = [parent for parent in candidates if parent.name not in {"body", "html"}]
                else:
                    candidates = node.select(selector)
                for candidate in candidates:
                    if not any(candidate is existing for existing in nodes):
                        nodes.append(candidate)
            return DomLocator(nodes)

        async def evaluate(self, script, selector, **kwargs):
            assert script == multi_field_totp_module._MULTI_FIELD_TOTP_SUBMIT_METADATA_JS
            scope = self.nodes[0]
            owner = scope if scope.name == "form" else None
            result = []
            for node in scope.select(selector):
                form = node.find_parent("form")
                form_attribute = node.get("form")
                matches = (
                    (form is owner and form_attribute in (None, owner.get("id")))
                    if owner
                    else (form is None and form_attribute is None)
                )
                result.append(
                    {
                        "tag": node.name,
                        "type": node.get("type"),
                        "has_form": owner is not None,
                        "form_matches": matches,
                        "disabled": node.has_attr("disabled"),
                        "aria_disabled": node.get("aria-disabled") == "true",
                        "visible": not node.has_attr("hidden"),
                        "label": node.get("aria-label") or node.get_text() or node.get("value") or "",
                    }
                )
            return result

        async def is_visible(self):
            return not self.nodes[0].has_attr("hidden")

        async def is_enabled(self, **kwargs):
            return not self.nodes[0].has_attr("disabled")

        async def get_attribute(self, name, **kwargs):
            return self.nodes[0].get(name)

        async def inner_text(self, **kwargs):
            return self.nodes[0].get_text()

    boxes_html = "".join(f'<input id="digit-{i}" maxlength="1">' for i in range(6))
    if markup == '<section>{boxes}<button id="own">Verify</button></section>':
        boxes_html = "".join(f'<form><input id="digit-{i}" maxlength="1"></form>' for i in range(6))
    newsletter = '<form id="newsletter"><button id="unrelated" type="submit">Subscribe</button></form>'
    soup = BeautifulSoup(
        f"<html><body>{markup.format(boxes=boxes_html, newsletter=newsletter)}</body></html>", "html.parser"
    )
    boxes = [DomLocator([soup.find(id=f"digit-{i}")]) for i in range(6)]
    controls = await multi_field_totp_module.find_multi_field_totp_submit_controls(boxes)
    assert (await controls[0].locator.get_attribute("id") if controls else None) == selected


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["db", "email"])
@pytest.mark.parametrize("obtained_offset", [0, None, 25])
async def test_external_retry_accepts_code_pushed_before_rejection_decision(
    monkeypatch: pytest.MonkeyPatch, source: str, obtained_offset: int | None
) -> None:
    obtained = datetime(2026, 9, 14, tzinfo=UTC)
    delivered = obtained + timedelta(seconds=5)
    pushed = (obtained + timedelta(seconds=10)).replace(tzinfo=None)
    decision = (obtained + timedelta(seconds=30)).replace(tzinfo=None)

    class Clock(datetime):
        now_value = decision

        @classmethod
        def utcnow(cls):
            return cls.now_value

    async def advance_poll(_seconds):
        Clock.now_value += timedelta(seconds=1 if Clock.now_value == decision else 1000)

    monkeypatch.setattr(otp_service, "datetime", Clock)
    monkeypatch.setattr(otp_service, "asyncio", ScopedAsyncio(sleep=advance_poll))
    monkeypatch.setattr(agent_module, "naive_utc_now", Clock.utcnow)
    monkeypatch.setattr(
        agent_module, "time", SimpleNamespace(time=lambda: Clock.now_value.replace(tzinfo=UTC).timestamp())
    )
    task = make_task(obtained, make_organization(obtained), totp_identifier="retry@example.test")
    step = make_step(obtained, task, step_id="step-retry", status=StepStatus.completed, order=0, output=None)
    state = _attempt()
    state.code_source = "external"
    state.external_code_obtained_at = (
        (obtained + timedelta(seconds=obtained_offset)).timestamp() if obtained_offset is not None else None
    )
    state.filled_at = delivered.timestamp()
    state.filled_code_hash = hashlib.sha256(_CODE.encode()).hexdigest()
    state.filled_url = task.url
    state.filled_loader_id = "loader"
    context = SkyvernContext(multi_field_totp={task.task_id: state}, totp_codes={f"{task.task_id}_totp_cache": _CODE})
    fresh = OTPValue(value="483917", type=OTPType.TOTP)
    cutoffs = []

    async def eligible_rows(**kwargs):
        cutoff = kwargs["created_after"]
        cutoffs.append(cutoff)
        rows = [
            SimpleNamespace(
                created_at=created_at,
                totp_code_id=f"row-{created_at.isoformat()}",
                code=code,
                otp_type=OTPType.TOTP,
                expired_at=None,
                task_id=None,
                workflow_run_id=None,
                workflow_id=None,
            )
            for created_at, code in [(pushed, fresh.value), (pushed + timedelta(seconds=2), _CODE)]
            if created_at >= cutoff
        ]
        return rows

    async def eligible_email(**kwargs):
        cutoff = kwargs["created_after"]
        cutoffs.append(cutoff)
        return fresh if pushed >= cutoff else None

    monkeypatch.setattr(
        otp_service.app.DATABASE.otp,
        "get_otp_codes",
        AsyncMock(side_effect=eligible_rows) if source == "db" else AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(otp_service.app.DATABASE.otp, "get_raw_otp_codes", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        otp_service.app.AGENT_FUNCTION,
        "get_otp_value_from_email",
        AsyncMock(side_effect=eligible_email) if source == "email" else AsyncMock(return_value=None),
    )
    page = SimpleNamespace(url=task.url)
    snapshot = SimpleNamespace(generate_scraped_page_without_screenshots=AsyncMock())
    snapshot.generate_scraped_page_without_screenshots.return_value = snapshot
    monkeypatch.setattr(agent_module, "_multi_field_totp_box_group", lambda *_: state.box_element_ids)
    monkeypatch.setattr(agent_module, "multi_field_totp_group_identity", lambda *_: "delivered-group")
    monkeypatch.setattr(agent_module, "get_main_document_loader_id", AsyncMock(return_value="loader"))
    monkeypatch.setattr(
        agent_module,
        "_refresh_multi_field_totp_group_binding",
        AsyncMock(return_value=(snapshot, state.box_element_ids)),
    )
    classifier = AsyncMock(return_value={"code_rejected": True, "reason": "Rejected"})
    monkeypatch.setattr(agent_module, "get_org_aware_primary_llm_api_handler", lambda: classifier)
    monkeypatch.setattr(
        agent_module.LLMAPIHandlerFactory, "get_override_llm_api_handler", lambda *args, **kwargs: classifier
    )
    monkeypatch.setattr(agent_module, "clear_multi_field_totp_boxes", AsyncMock(return_value=True))
    fill = AsyncMock(return_value=ActionSuccess())
    monkeypatch.setattr(agent_module, "_fill_multi_field_totp_group", fill)
    monkeypatch.setattr(agent_module, "submit_multi_field_totp_retry", AsyncMock(return_value=True))
    with skyvern_context.scoped(context):
        result = await ForgeAgent()._maybe_retry_multi_field_totp_after_rejection(
            task, step, page, snapshot, "The one-time code was rejected."
        )
    expected_cutoff = (obtained if obtained_offset == 0 else delivered).replace(tzinfo=None)
    assert cutoffs and all(cutoff == expected_cutoff for cutoff in cutoffs)
    assert result == multi_field_totp_module.RetryOutcome.RETRIED
    assert fill.await_args.args[-1] == fresh.value
    assert context.multi_field_totp_rejections[task.task_id].rejected_at == decision
    assert context.multi_field_totp_rejections[task.task_id].rejected_code_obtained_at == expected_cutoff
    assert state.external_code_obtained_at == Clock.now_value.replace(tzinfo=UTC).timestamp()


@pytest.mark.asyncio
@pytest.mark.parametrize("cache_path", ["payload", "resolver_armed", "resolver_unarmed", "resolver_unarmed_formatted"])
async def test_external_code_acquisition_time_survives_replanning(
    monkeypatch: pytest.MonkeyPatch, cache_path: str
) -> None:
    clock = SimpleNamespace(value=100.0)
    monkeypatch.setattr(agent_module, "time", SimpleNamespace(time=lambda: clock.value))
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), navigation_payload={})
    step = make_step(now, task, step_id="step", status=StepStatus.created, order=0, output=None)
    snapshot = _box_page(6)
    agent = ForgeAgent()
    context = SkyvernContext(task_id=task.task_id)
    if cache_path == "resolver_armed":
        state = _attempt()
        state.code_source = "external"
        context.multi_field_totp[task.task_id] = state
    resolved = AsyncMock(return_value=OTPValue(value=_CODE, type=OTPType.TOTP))
    monkeypatch.setattr(agent_module, "resolve_otp_value", resolved)
    monkeypatch.setattr(agent_module.service_utils, "is_cua_task", AsyncMock(return_value=False))
    llm = AsyncMock(return_value={})
    monkeypatch.setattr(agent_module, "get_org_aware_primary_llm_api_handler", lambda: llm)
    monkeypatch.setattr(agent_module.LLMAPIHandlerFactory, "get_override_llm_api_handler", lambda *args, **kwargs: llm)

    async def build_prompt(*args, **kwargs):
        clock.value += 3
        agent._build_navigation_payload(task, step=step, scraped_page=snapshot)
        return SimpleNamespace(
            prompt="", prompt_name="extract-action", use_caching=False, without_page_information=True
        )

    monkeypatch.setattr(agent, "_build_extract_action_prompt", build_prompt)
    with skyvern_context.scoped(context):
        for at, code, expected in [(100.0, _CODE, 100.0), (200.0, _CODE, 100.0), (300.0, "483917", 300.0)]:
            clock.value = at
            if cache_path == "payload":
                task.navigation_payload = {"verification_code": code}
                agent._build_navigation_payload(task, step=step, scraped_page=snapshot)
            else:
                resolved.return_value = OTPValue(
                    value=f"{code[:3]} {code[3:]}" if cache_path.endswith("formatted") else code, type=OTPType.TOTP
                )
                await agent.handle_potential_verification_code(
                    task, step, snapshot, SimpleNamespace(), {"place_to_enter_verification_code": True}
                )
            state = context.multi_field_totp[task.task_id]
            assert context.totp_codes[f"{task.task_id}_totp_cache"] == code
            assert state.external_code_obtained_at == expected
        context.reset_attempt(task.task_id)
        assert state.external_code_obtained_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("full_before_clear", [True, False])
@pytest.mark.parametrize("remount", [False, True, "container", "hidden_aggregate"])
async def test_group_fill_does_not_repeat_a_consumed_auto_submit(
    monkeypatch: pytest.MonkeyPatch, full_before_clear: bool, remount: bool | str
) -> None:
    elements = _fake_group_elements(4)
    values = [""] * 4
    state = MultiFieldTotpAttempt([f"box-{i}" for i in range(4)], 4, "external")
    streamed = False
    listeners = []
    monkeypatch.setattr(
        handler,
        "install_multi_field_totp_fill_observer",
        multi_field_totp_module.install_multi_field_totp_fill_observer,
    )

    async def stream(_code):
        nonlocal streamed
        streamed = True
        observed = _probe_live_fill_observer(remount=remount, clear_at=4 if full_before_clear else 2)
        prefix = elements[0].get_locator().evaluate.await_args.args[1]["prefix"]
        for count in observed["counts"]:
            listeners[0](SimpleNamespace(text=prefix + count.split(":")[1]))

    for i, element in enumerate(elements):
        element.get_locator().evaluate = AsyncMock(return_value=None)
        element.get_locator().input_value = AsyncMock(side_effect=lambda index=i, **_: values[index])
        element.input_fill.side_effect = lambda value, index=i: values.__setitem__(index, value)
    monkeypatch.setattr(handler, "_resolve_multi_field_totp_group_elements", AsyncMock(return_value=elements))
    monkeypatch.setattr(handler, "_apply_secret_visual_mask_if_needed", AsyncMock())
    monkeypatch.setattr(handler, "get_main_document_loader_id", AsyncMock(return_value="loader"))
    monkeypatch.setattr(handler, "asyncio", ScopedAsyncio(sleep=AsyncMock()))
    monkeypatch.setattr(handler, "_read_multi_field_totp_values", AsyncMock(side_effect=lambda _: list(values)))
    monkeypatch.setattr(multi_field_totp_module, "asyncio", ScopedAsyncio(sleep=AsyncMock()))
    monkeypatch.setattr(multi_field_totp_module, "get_main_document_loader_id", AsyncMock(return_value="loader"))
    monkeypatch.setattr(
        multi_field_totp_module,
        "_retry_box_locators",
        AsyncMock(return_value=[element.get_locator() for element in elements]),
    )
    widget = SimpleNamespace(
        evaluate=AsyncMock(
            side_effect=lambda *a, **kw: {
                "root": "new-input-error" if streamed and not full_before_clear else "retained-invalid-alert"
            }
        )
    )
    monkeypatch.setattr(multi_field_totp_module, "_multi_field_totp_widget_scopes", AsyncMock(return_value=[widget]))
    page = SimpleNamespace(
        url="same",
        keyboard=SimpleNamespace(type=AsyncMock(side_effect=stream)),
        on=lambda event, callback: listeners.append(callback),
        remove_listener=lambda event, callback: listeners.remove(callback),
    )
    snapshot = _box_page(4)
    snapshot.url, snapshot._document_loader_id = "same", "loader"
    result = await _fill_multi_field_totp_group(page, snapshot, _fill_task(), state, "1234")
    assert isinstance(result, ActionSuccess)
    if full_before_clear:
        assert all(not element.input_fill.await_args_list for element in elements)
        assert result.data["totp_submission_observed"]
        assert not state.fill_verified
    else:
        assert [element.input_fill.await_args.args[0] for element in elements] == list("1234")
        assert state.fill_verified
        assert not result.data.get("totp_submission_observed")
    assert state.filled_code_hash == hashlib.sha256(b"1234").hexdigest()
    assert state.filled_at is not None


@pytest.mark.asyncio
async def test_retry_barrier_propagates_parent_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now))
    step = make_step(now, task, step_id="step", status=StepStatus.completed, order=0, output=None)
    attempt = _attempt()
    attempt.filled_code_hash = hashlib.sha256(_CODE.encode()).hexdigest()
    attempt.filled_at = now.timestamp()
    attempt.filled_url = "same"
    attempt.filled_loader_id = "loader"
    context = SkyvernContext(multi_field_totp={task.task_id: attempt})
    monkeypatch.setattr(agent_module, "get_main_document_loader_id", AsyncMock(return_value="loader"))
    classifier = AsyncMock(return_value={"code_rejected": True, "reason": "Rejected"})
    monkeypatch.setattr(agent_module, "get_org_aware_primary_llm_api_handler", lambda: classifier)
    monkeypatch.setattr(
        agent_module.LLMAPIHandlerFactory, "get_override_llm_api_handler", lambda *args, **kwargs: classifier
    )
    started, cleaning = asyncio.Event(), asyncio.Event()

    async def speculate():
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            await asyncio.Future()

    speculative = asyncio.create_task(speculate())
    await started.wait()
    with skyvern_context.scoped(context):
        parent = asyncio.create_task(
            ForgeAgent()._maybe_retry_multi_field_totp_after_rejection(
                task,
                step,
                SimpleNamespace(url="same"),
                _box_page(6),
                "The code was rejected.",
                speculative_task=speculative,
            )
        )
        await cleaning.wait()
        parent.cancel()
        with pytest.raises(asyncio.CancelledError):
            await parent
    assert classifier.await_args is None


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected_code", ["QWERTY", _CODE])
async def test_rejection_classifier_masks_alphabetic_codes_and_includes_referents(
    monkeypatch: pytest.MonkeyPatch,
    rejected_code: str,
) -> None:
    now = datetime.now(UTC)
    task = make_task(
        now,
        make_organization(now),
        navigation_goal=f"Enter {rejected_code} and terminate if the passcode is invalid.",
        complete_criterion=f"Code {rejected_code} accepted",
        terminate_criterion=f"Code {rejected_code} invalid",
    )
    step = make_step(now, task, step_id="step", status=StepStatus.completed, order=0, output=None)
    context = SkyvernContext(
        multi_field_totp_rejections={
            task.task_id: MultiFieldTotpRejection(
                hashlib.sha256(rejected_code.encode()).hexdigest(),
                now,
                None,
                "Rejected",
                retry_used=True,
                submitted_at=now,
                hint_code="MASKED",
            )
        }
    )
    render = MagicMock(wraps=agent_module.prompt_engine.load_prompt)
    monkeypatch.setattr(agent_module.prompt_engine, "load_prompt", render)
    classifier = AsyncMock(return_value={"code_rejected": True, "reason": "Code rejected"})
    monkeypatch.setattr(agent_module, "get_org_aware_primary_llm_api_handler", lambda: classifier)
    monkeypatch.setattr(
        agent_module.LLMAPIHandlerFactory, "get_override_llm_api_handler", lambda *args, **kwargs: classifier
    )
    with skyvern_context.scoped(context), structlog.testing.capture_logs() as logs:
        outcome = await ForgeAgent()._maybe_retry_multi_field_totp_after_rejection(
            task,
            step,
            None,
            None,
            f"Code {rejected_code} was rejected; prefix650294suffix.",
            [{"reason": f"Invalid {rejected_code}"}],
            intention=f"Check {rejected_code}; prefix650294suffix.",
            response=f"Invalid {rejected_code}; prefix650294suffix.",
            matched_error_reasoning=[
                f"The submitted {rejected_code} is invalid; prefix650294suffix.",
                f"The submitted {rejected_code} is expired; other483917text.",
            ],
        )
    assert outcome == multi_field_totp_module.RetryOutcome.SECOND_REJECTION
    fields = render.call_args.kwargs
    assert all(
        fields[name]
        for name in (
            "reasoning",
            "navigation_goal",
            "complete_criterion",
            "terminate_criterion",
            "failure_categories",
            "matched_error_reasoning",
        )
    )
    assert rejected_code not in classifier.await_args.kwargs["prompt"]
    assert rejected_code not in json.dumps(fields)
    assert rejected_code not in str(logs)
    assert classifier.await_args.kwargs["screenshots"] == []
    rendered = BeautifulSoup(classifier.await_args.kwargs["prompt"], "html.parser")
    for tag in ("termination_reasoning", "action_intention", "action_response", "matched_error_reasoning"):
        text = rendered.find(tag).get_text()
        assert "MASKED" in text, "Known forms must be replaced before the blanket digit mask"
        assert "prefix******suffix" in text
        assert not any(character in "0123456789" for character in text)
    assert "other******text" in rendered.find("matched_error_reasoning").get_text()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "signal", ["countdown", "sidebar", "remount", "disabled", "visible_alert", "volatile_ancestor"]
)
async def test_submission_evidence_filters_volatile_nodes_and_rebinds_scopes(
    monkeypatch: pytest.MonkeyPatch, signal: str
) -> None:
    state = _attempt()
    page = SimpleNamespace(url="same")
    original = SimpleNamespace(url="same", _document_loader_id="loader")
    fresh = SimpleNamespace(url="same", _document_loader_id="loader")
    old_box = SimpleNamespace(count=AsyncMock(return_value=1), input_value=AsyncMock(return_value="x"))
    new_box = SimpleNamespace(count=AsyncMock(return_value=1), input_value=AsyncMock(return_value="x"))
    first = {"root": "section", "root/0": "boxes", "root/1": "count-30", "root/2": "feedback-hidden"}
    second = {**first, "root/1": "count-29"}
    after = {**second, "root/1": "count-28"}
    if signal == "volatile_ancestor":
        second["root"], after["root"] = "loading-state-1", "loading-state-2"
    if signal in {"sidebar", "remount", "visible_alert", "volatile_ancestor"}:
        after["root/2"] = "feedback-visible"
    if signal == "disabled":
        after["root/0"] = "boxes-disabled"
    inner = SimpleNamespace(evaluate=AsyncMock(side_effect=AssertionError("Feedback lives outside the box container")))
    outer = SimpleNamespace(evaluate=AsyncMock(side_effect=[first, second, second, after]))
    rebound = SimpleNamespace(evaluate=AsyncMock(return_value=after))

    async def scopes(boxes, *, include_ancestors=False):
        assert include_ancestors
        return [inner, rebound if boxes == [new_box] else outer]

    monkeypatch.setattr(multi_field_totp_module, "_multi_field_totp_widget_scopes", scopes)
    monkeypatch.setattr(multi_field_totp_module, "get_main_document_loader_id", AsyncMock(return_value="loader"))
    monkeypatch.setattr(multi_field_totp_module, "asyncio", ScopedAsyncio(sleep=AsyncMock()))
    monkeypatch.setattr(
        multi_field_totp_module,
        "_retry_box_locators",
        AsyncMock(side_effect=lambda _page, snapshot, _ids: [new_box] if snapshot is fresh else [old_box]),
    )
    monkeypatch.setattr(
        multi_field_totp_module, "_refresh_multi_field_totp_group_binding", AsyncMock(return_value=(fresh, ["new-box"]))
    )
    baseline = await multi_field_totp_module.capture_multi_field_totp_submission_baseline(page, [old_box])
    assert baseline is not None
    if signal == "remount":
        old_box.count.return_value = 0
        outer.evaluate.side_effect = PlaywrightTimeoutError("Old widget is detached")
    result = await multi_field_totp_module.multi_field_totp_submission_evidence(
        page, original, state, baseline=baseline, post_dispatch=True
    )
    assert result == (signal != "countdown")
    if signal == "remount":
        assert rebound.evaluate.await_args is not None


def test_widget_probe_hashes_own_nodes_without_secret_values_or_descendant_churn() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the DOM probe without a browser")
    program = r"""
const probe = PROBE;
globalThis.getComputedStyle = () => ({visibility: 'visible', display: 'block'});
const element = (tagName, text = '', attributes = [], children = []) => ({
    tagName, attributes, children, childNodes: [{nodeType: 3, textContent: text}],
    isConnected: true, getClientRects: () => [1],
});
const ticker = element('P', 'Resend in 30');
const input = element('INPUT', '', [{name: 'value', value: 'QWERTY'}, {name: 'unique_id', value: 'old'}]);
const alert = element('P', '', [{name: 'role', value: 'alert'}]);
const root = element('FORM', '', [], [input, ticker, alert]);
const before = probe(root);
ticker.childNodes[0].textContent = 'Resend in 29';
input.attributes[0].value = 'ASDFGH';
input.attributes[1].value = 'remounted';
const ticking = probe(root);
alert.childNodes[0].textContent = 'The code was rejected';
const feedback = probe(root);
process.stdout.write(JSON.stringify({before, ticking, feedback}));
""".replace("PROBE", multi_field_totp_module._MULTI_FIELD_TOTP_WIDGET_FINGERPRINT_JS)
    result = subprocess.run([node, "-e", program], check=True, capture_output=True, text=True, timeout=10)
    snapshots = json.loads(result.stdout)
    before, ticking, feedback = (snapshots[key] for key in ("before", "ticking", "feedback"))
    assert before["root"] == ticking["root"] == feedback["root"]
    assert before["root/0"] == ticking["root/0"] == feedback["root/0"]
    assert before["root/1"] != ticking["root/1"]
    assert ticking["root/2"] != feedback["root/2"]
    assert "QWERTY" not in result.stdout and "ASDFGH" not in result.stdout


@pytest.mark.asyncio
@pytest.mark.parametrize("replaced_widget", [False, True])
async def test_retry_rediscovery_compares_delivered_structure_before_binding(
    monkeypatch: pytest.MonkeyPatch, replaced_widget: bool
) -> None:
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now))
    step = make_step(now, task, step_id="step", status=StepStatus.completed, order=0, output=None)
    original = _box_page(6, frame="main.frame")
    fresh = _box_page(6, frame="main.frame", input_type="tel" if replaced_widget else "text")
    for index, element in enumerate(fresh.element_tree[0]["children"]):
        element["id"] = f"new-{index}"
    for snapshot in (original, fresh):
        snapshot.url = task.url
        snapshot.id_to_element_dict = {element["id"]: element for element in snapshot.elements}
        snapshot.generate_scraped_page_without_screenshots = AsyncMock(return_value=fresh)
    state = _attempt()
    state.filled_group_identity = multi_field_totp_module.multi_field_totp_group_identity(original, 6)
    state.filled_code_hash = hashlib.sha256(_CODE.encode()).hexdigest()
    state.filled_at, state.filled_url, state.filled_loader_id = 1.0, task.url, "loader"
    context = SkyvernContext(multi_field_totp={task.task_id: state})
    page = SimpleNamespace(url=task.url)

    async def bind(snapshot, _page, attempt, **kwargs):
        assert snapshot is fresh
        assert all(element_id in snapshot.id_to_element_dict for element_id in attempt.box_element_ids)
        return snapshot, attempt.box_element_ids

    monkeypatch.setattr(agent_module, "_refresh_multi_field_totp_group_binding", bind)
    monkeypatch.setattr(agent_module, "get_main_document_loader_id", AsyncMock(return_value="loader"))
    classifier = AsyncMock(return_value={"code_rejected": True, "reason": "Rejected"})
    monkeypatch.setattr(agent_module, "get_org_aware_primary_llm_api_handler", lambda: classifier)
    monkeypatch.setattr(
        agent_module.LLMAPIHandlerFactory, "get_override_llm_api_handler", lambda *args, **kwargs: classifier
    )
    monkeypatch.setattr(agent_module, "_resolve_multi_field_totp_code", AsyncMock(return_value="483917"))
    clear = AsyncMock(return_value=True)
    monkeypatch.setattr(agent_module, "clear_multi_field_totp_boxes", clear)
    monkeypatch.setattr(agent_module, "_fill_multi_field_totp_group", AsyncMock(return_value=ActionSuccess()))
    monkeypatch.setattr(agent_module, "submit_multi_field_totp_retry", AsyncMock(return_value=True))
    with skyvern_context.scoped(context):
        outcome = await ForgeAgent()._maybe_retry_multi_field_totp_after_rejection(
            task, step, page, original, "Invalid code"
        )
    assert outcome == (
        multi_field_totp_module.RetryOutcome.NO_RETRY
        if replaced_widget
        else multi_field_totp_module.RetryOutcome.RETRIED
    )
    if replaced_widget:
        assert clear.await_args is None
    else:
        assert clear.await_args.args[1] is fresh


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["payload", "transient", "resolution"])
@pytest.mark.parametrize(("supplied", "accepted"), [("AB-12C", None), ("１２３４５６", "123456"), ("ABé12C", None)])
async def test_multi_box_external_code_format_admission(
    monkeypatch: pytest.MonkeyPatch, entry: str, supplied: str, accepted: str | None
) -> None:
    now = datetime.now(UTC)
    task = make_task(
        now, make_organization(now), navigation_payload={"verification_code": supplied} if entry == "payload" else {}
    )
    state = _attempt()
    state.code_source = "external"
    context = SkyvernContext(task_id=task.task_id, multi_field_totp={task.task_id: state})
    if entry != "payload":
        context.totp_codes[task.task_id if entry == "transient" else f"{task.task_id}_totp_cache"] = supplied
    with skyvern_context.scoped(context), structlog.testing.capture_logs() as logs:
        if entry == "resolution":
            result = await _resolve_multi_field_totp_code(task, state)
            if accepted is None:
                assert isinstance(result, ActionFailure)
            else:
                assert result == accepted
        else:
            ForgeAgent()._build_navigation_payload(task, step=SimpleNamespace(), scraped_page=_box_page(6))
    assert context.totp_codes.get(f"{task.task_id}_totp_cache") == accepted
    assert state.filled_code_hash is None
    if accepted is None:
        assert any(entry.get("reason") == "unsupported_code_format" for entry in logs)
    assert supplied not in str(logs)


@pytest.mark.asyncio
@pytest.mark.parametrize("supplied", ["AB-12C", "ABé12C", "１２３４５６"])
async def test_first_plan_format_gate_requires_an_accepted_code(monkeypatch: pytest.MonkeyPatch, supplied: str) -> None:
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), navigation_payload={})
    task.totp_identifier = "identifier"
    step = make_step(now, task, step_id="step", status=StepStatus.created, order=0, output=None)
    state = _attempt()
    state.code_source = "external"
    context = SkyvernContext(
        multi_field_totp={task.task_id: state}, totp_codes={f"{task.task_id}_totp_cache": supplied}
    )
    agent = ForgeAgent()
    resolve = AsyncMock(return_value={"actions": []})
    monkeypatch.setattr(agent, "handle_potential_verification_code", resolve)
    response = {
        "place_to_enter_verification_code": True,
        "should_enter_verification_code": True,
        "actions": [
            {"action_type": "input_text", "element_id": element_id, "text": state.hint_code[index]}
            for index, element_id in enumerate(state.box_element_ids)
        ],
    }
    with skyvern_context.scoped(context):
        await agent.handle_potential_OTP_actions(task, step, _box_page(6), SimpleNamespace(), response)
    accepted = supplied == "１２３４５６"
    assert (resolve.await_args is None) is accepted
    assert context.totp_codes.get(f"{task.task_id}_totp_cache") == ("123456" if accepted else None)


@pytest.mark.parametrize("representation", ["１２３４５６", "ⒶⒷⒸⒹⒺⒻ"])
def test_normalized_rejected_code_is_masked_after_teardown(representation: str) -> None:
    context = SkyvernContext()
    with skyvern_context.scoped(context):
        normalized = skyvern_context.normalize_multi_field_totp_code(representation, 6)
        assert normalized is not None
        context.multi_field_totp_rejections["task"] = MultiFieldTotpRejection(
            hashlib.sha256(normalized.encode()).hexdigest(),
            datetime.now(UTC),
            None,
            "Rejected",
            hint_code=_HINT,
        )
        fields = agent_module._replace_multi_field_totp_prompt_code(
            "task",
            {"navigation_goal": f"Use {representation}; stop on rejection."},
        )
    assert representation not in fields["navigation_goal"]
    assert normalized not in fields["navigation_goal"]
    assert _HINT in fields["navigation_goal"]


@pytest.mark.asyncio
@pytest.mark.parametrize("matches", [True, False])
async def test_fill_observer_unavailable_uses_value_readback(monkeypatch: pytest.MonkeyPatch, matches: bool) -> None:
    monkeypatch.setattr(handler, "install_multi_field_totp_fill_observer", AsyncMock(return_value=None))
    elements = _fake_group_elements(4)
    monkeypatch.setattr(handler, "_resolve_multi_field_totp_group_elements", AsyncMock(return_value=elements))
    monkeypatch.setattr(handler, "_apply_secret_visual_mask_if_needed", AsyncMock())
    monkeypatch.setattr(handler, "get_main_document_loader_id", AsyncMock(return_value="loader"))
    monkeypatch.setattr(
        handler,
        "_read_multi_field_totp_values",
        AsyncMock(side_effect=[list("0000"), list("1234") if matches else list("0000"), list("0000")]),
    )
    page = SimpleNamespace(url="same", keyboard=SimpleNamespace(type=AsyncMock()))
    state = MultiFieldTotpAttempt([f"box-{i}" for i in range(4)], 4, "external")
    result = await _fill_multi_field_totp_group(page, _box_page(4), _fill_task(), state, "1234")
    assert result.success is matches
    assert page.keyboard.type.await_args.args[0] == "1234"
    assert state.fill_verified is matches
    if matches:
        assert all(element.input_fill.await_args is None for element in elements)
    else:
        assert [element.input_fill.await_args.args[0] for element in elements] == list("1234")
        assert state.observed_max_filled == 0
        assert state.filled_code_hash is None


@pytest.mark.asyncio
async def test_fill_observer_preserves_only_counts_across_document_teardown() -> None:
    state = _attempt()
    page = SimpleNamespace(on=MagicMock(), remove_listener=MagicMock())
    box = SimpleNamespace(evaluate=AsyncMock(), count=AsyncMock(return_value=1))
    observer = await multi_field_totp_module.install_multi_field_totp_fill_observer(page, [box], state)
    assert observer is not None
    callback = page.on.call_args.args[1]
    callback(SimpleNamespace(text=observer.prefix + "6"))
    for message in ("other:6", observer.prefix + "7", observer.prefix + "not-a-count"):
        callback(SimpleNamespace(text=message))
    box.evaluate.side_effect = PlaywrightError("Execution context was destroyed")
    await observer.refresh([box])
    assert state.observed_max_filled == 6
    observer.stop()
    assert page.remove_listener.call_args.args == ("console", callback)
    replacement = await multi_field_totp_module.install_multi_field_totp_fill_observer(page, [box], state)
    assert replacement is None
    assert state.observed_max_filled == 0


def test_input_observer_measures_peak_fullness_before_site_clear() -> None:
    for clear_at in (2, 4):
        observed = _probe_live_fill_observer(remount=False, clear_at=clear_at)
        assert observed["max"] == clear_at
        assert observed["empty"]
        assert observed["submissions"] == (1 if clear_at == 4 else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("semantic", [False, True])
async def test_slow_ticker_after_noop_submit_needs_feedback_semantics(
    monkeypatch: pytest.MonkeyPatch, semantic: bool
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the DOM probe without a browser")
    program = r"""
const probe = PROBE;
globalThis.getComputedStyle = () => ({visibility: 'visible', display: 'block'});
const ticker = {
    tagName: 'P', attributes: SEMANTIC ? [{name: 'aria-live', value: 'polite'}] : [], children: [],
    childNodes: [{nodeType: 3, textContent: 'Resend in 30s (0:30)'}],
    isConnected: true, getClientRects: () => [1],
};
const before = probe(ticker);
ticker.childNodes[0].textContent = 'Resend in 29s (0:29)';
process.stdout.write(JSON.stringify({before, after: probe(ticker)}));
""".replace("PROBE", multi_field_totp_module._MULTI_FIELD_TOTP_WIDGET_FINGERPRINT_JS).replace(
        "SEMANTIC", json.dumps(semantic)
    )
    result = subprocess.run([node, "-e", program], check=True, capture_output=True, text=True, timeout=10)
    snapshots = json.loads(result.stdout)
    elapsed = 0.0
    submitted = False
    samples = []

    async def sleep(seconds):
        nonlocal elapsed
        elapsed += seconds

    async def snapshot(*args, **kwargs):
        if not submitted:
            samples.append(elapsed)
        return snapshots["after" if submitted else "before"]

    async def click(**kwargs):
        nonlocal submitted
        if not kwargs.get("trial"):
            assert kwargs["no_wait_after"]
            submitted = True

    state = _attempt()
    page = SimpleNamespace(url="same")
    scraped = SimpleNamespace(url="same", _document_loader_id="loader")
    box = SimpleNamespace(count=AsyncMock(return_value=1), input_value=AsyncMock(return_value="*"))
    control = SimpleNamespace(
        count=AsyncMock(return_value=1),
        click=click,
        get_attribute=AsyncMock(return_value=None),
        is_visible=AsyncMock(return_value=True),
        is_enabled=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(multi_field_totp_module, "asyncio", ScopedAsyncio(sleep=sleep))
    monkeypatch.setattr(multi_field_totp_module, "_retry_box_locators", AsyncMock(return_value=[box]))
    monkeypatch.setattr(multi_field_totp_module, "_multi_field_totp_widget_snapshot", snapshot)
    monkeypatch.setattr(multi_field_totp_module, "_multi_field_totp_widget_scopes", AsyncMock(return_value=[object()]))
    monkeypatch.setattr(multi_field_totp_module, "get_main_document_loader_id", AsyncMock(return_value="loader"))
    monkeypatch.setattr(
        multi_field_totp_module,
        "find_multi_field_totp_submit_controls",
        AsyncMock(return_value=[_submit_candidate(control)]),
    )
    monkeypatch.setattr(
        multi_field_totp_module,
        "_refresh_multi_field_totp_group_binding",
        AsyncMock(return_value=(scraped, state.box_element_ids)),
    )
    assert await multi_field_totp_module.submit_multi_field_totp_retry(page, scraped, state) is semantic
    assert [round(value - samples[0], 1) for value in samples] == [0.0, 0.6, 1.2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["tie", "rank", "disabled", "aria_disabled", "hidden", "covered", "enter", "enter_noop", "click_error"],
)
async def test_retry_trials_ranked_controls_and_logs_dispatch(monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    state = _attempt()
    full = False
    dispatched = False
    trials = []
    clicks = []
    form = SimpleNamespace(count=AsyncMock(return_value=1), get_attribute=AsyncMock(return_value="otp"))
    form.filter = lambda **_: form
    name = "Verify " + "the authentication challenge " * 3

    def control(identifier, label, *, input_type="submit"):
        async def attribute(key, **kwargs):
            if key == "disabled":
                return "" if identifier == "first" and (not full or case == "disabled") else None
            if key == "aria-disabled":
                return "true" if identifier == "first" and case == "aria_disabled" else None
            return {"type": input_type, "aria-label": label}.get(key)

        async def click(**kwargs):
            nonlocal dispatched
            assert full
            if kwargs.get("trial"):
                trials.append(identifier)
                if case in {"enter", "enter_noop"} or (identifier == "first" and case == "covered"):
                    raise PlaywrightTimeoutError("Secret-bearing exception text must not be logged")
            else:
                assert kwargs["no_wait_after"]
                clicks.append(identifier)
                if case == "click_error":
                    raise PlaywrightTimeoutError("Secret-bearing exception text must not be logged")
                dispatched = True

        return SimpleNamespace(
            count=AsyncMock(return_value=1),
            locator=lambda _: form,
            get_attribute=attribute,
            inner_text=AsyncMock(return_value=label),
            is_visible=AsyncMock(return_value=not (identifier == "first" and case == "hidden")),
            is_enabled=AsyncMock(side_effect=lambda **_: full),
            click=click,
            evaluate=AsyncMock(return_value={"tag": "button", "type": input_type}),
            aria_snapshot=AsyncMock(return_value=f"- button {json.dumps(label)}"),
        )

    first = control("first", name, input_type="button" if case == "rank" else "submit")
    second = control("second", name)
    controls = SimpleNamespace(count=AsyncMock(return_value=2), nth=lambda index: [first, second][index])
    form.locator = lambda _: controls

    async def batch_metadata(*args, **kwargs):
        return [
            {
                "tag": "button",
                "type": await item.get_attribute("type"),
                "label": await item.get_attribute("aria-label"),
                "has_form": True,
                "form_matches": True,
                "disabled": await item.get_attribute("disabled") is not None,
                "aria_disabled": await item.get_attribute("aria-disabled") == "true",
                "visible": await item.is_visible(),
            }
            for item in (first, second)
        ]

    form.evaluate = batch_metadata

    async def enter(*args, **kwargs):
        nonlocal dispatched
        assert args == ("Enter",) and "no_wait_after" not in kwargs
        dispatched = case == "enter"

    box = SimpleNamespace(
        count=AsyncMock(return_value=1),
        locator=lambda _: form,
        press=enter,
        evaluate=AsyncMock(return_value={"tag": "input", "type": "text"}),
    )
    page = SimpleNamespace(url="same")
    scraped = SimpleNamespace(url="same", _document_loader_id="loader")
    monkeypatch.setattr(multi_field_totp_module, "_multi_field_totp_widget_scopes", AsyncMock(return_value=[form]))
    monkeypatch.setattr(multi_field_totp_module, "_retry_box_locators", AsyncMock(return_value=[box]))
    monkeypatch.setattr(
        multi_field_totp_module,
        "_refresh_multi_field_totp_group_binding",
        AsyncMock(return_value=(scraped, state.box_element_ids)),
    )
    monkeypatch.setattr(multi_field_totp_module, "get_main_document_loader_id", AsyncMock(return_value="loader"))
    monkeypatch.setattr(
        multi_field_totp_module, "capture_multi_field_totp_submission_baseline", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        multi_field_totp_module,
        "multi_field_totp_submission_evidence",
        AsyncMock(side_effect=lambda *a, **k: dispatched),
    )
    monkeypatch.setattr(multi_field_totp_module, "asyncio", ScopedAsyncio(sleep=AsyncMock()))
    # The page enables the first control only after the burst completes.
    full = True
    with (
        skyvern_context.scoped(SkyvernContext(task_id="task", workflow_run_id="workflow", step_id="step")),
        structlog.testing.capture_logs() as logs,
    ):
        if case in {"enter_noop", "click_error"}:
            expected = (
                multi_field_totp_module.MultiFieldTotpSubmitControlNotActionable
                if case == "enter_noop"
                else PlaywrightTimeoutError
            )
            with pytest.raises(expected):
                await multi_field_totp_module.submit_multi_field_totp_retry(page, scraped, state)
        else:
            assert await multi_field_totp_module.submit_multi_field_totp_retry(page, scraped, state)
    if case in {"disabled", "aria_disabled", "hidden", "rank"}:
        assert trials == ["second"] and clicks == ["second"]
    elif case == "covered":
        assert trials == ["first", "second"] and clicks == ["second"]
    elif case in {"enter", "enter_noop"}:
        assert trials == ["first", "second"] and clicks == []
    else:
        assert trials == ["first"] and clicks == ["first"]
    events = [entry for entry in logs if entry["event"] == "Multi-field TOTP submit dispatch"]
    assert events
    for entry in events:
        assert (entry["task_id"], entry["workflow_run_id"], entry["step_id"]) == ("task", "workflow", "step")
        assert entry["phase"] in {"trial", "click", "enter"}
        assert entry["control_tag"] in {"button", "input"}
        if entry["phase"] != "enter":
            assert entry["control_type"] in {"submit", "button"}
            assert entry["rank_word"] == "verify"
            assert isinstance(entry["candidate_index"], int)
        assert "control_name" not in entry
        assert name not in str(entry)
    assert "Secret-bearing exception text" not in str(logs)
    if case == "covered":
        assert any(
            entry["phase"] == "trial" and entry["status"] == "failed" and entry["error_type"] == "TimeoutError"
            for entry in events
        )
    if case in {"enter", "enter_noop"}:
        assert any(entry["phase"] == "enter" and entry["status"] == "succeeded" for entry in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["ABC DEF", "123 456", "ＡＢＣＤＥＦ"])
async def test_submit_control_diagnostics_never_log_page_text(spelling: str) -> None:
    label = f"Verify {spelling}"
    fields = multi_field_totp_module._multi_field_totp_submit_control_log_fields(
        {"tag": "button", "type": "submit", "label": label}, candidate_index=2
    )
    assert fields == {
        "control_tag": "button",
        "control_type": "submit",
        "rank_word": "verify",
        "submit_type": True,
        "candidate_index": 2,
    }
    assert all(len(value) <= len("continue") for value in fields.values() if isinstance(value, str))
    assert spelling not in str(fields)


@pytest.mark.asyncio
async def test_unsupported_payload_candidate_stays_private_and_cannot_use_ordinary_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), navigation_payload={"verification_code": "AB-12C"})
    task.navigation_goal = "Use AB-12C (AB12C); preserve password paﬀword."
    step = make_step(now, task, step_id="step", status=StepStatus.created, order=0, output=None)
    context = SkyvernContext(task_id=task.task_id, multi_field_totp={task.task_id: _attempt()})
    agent = ForgeAgent()
    snapshot = _box_page(6)
    monkeypatch.setattr(agent_module, "resolve_otp_value", AsyncMock(return_value=None))
    monkeypatch.setattr(
        handler, "DomUtil", MagicMock(side_effect=AssertionError("Unsupported code reached ordinary input"))
    )
    with skyvern_context.scoped(context):
        payload = agent._build_navigation_payload(task, step=step, scraped_page=snapshot)
        fields = agent_module._replace_multi_field_totp_prompt_code(
            task.task_id, {"navigation_goal": task.navigation_goal}
        )
        assert {"AB-12C", "AB12C"} <= context.runtime_secret_values
        assert "paﬀword" in fields["navigation_goal"]
        for text in ("AB-12C", "AB12C"):
            action = InputTextAction(text=text, element_id="box-0", task_id=task.task_id, reasoning=f"Enter {text}")
            agent_module._maybe_arm_multi_field_totp(action, task.task_id, carries_expected_value=False)
            result = await handler._handle_input_text_action(action, SimpleNamespace(), snapshot, task, step)
            assert isinstance(result[0], ActionFailure)
            assert result[0].skip_remaining_actions
            persisted = action_for_multi_field_totp_persistence(action)
            assert persisted.text == text
            assert persisted.reasoning == f"Enter {text}"
            assert persisted.totp_timing_info == {"is_totp_sequence": True}
        response = await agent.handle_potential_verification_code(
            task,
            step,
            snapshot,
            SimpleNamespace(),
            {
                "place_to_enter_verification_code": True,
                "actions": [
                    {"action_type": "input_text", "element_id": "box-0", "text": "AB-12C", "reasoning": "Use AB12C"}
                ],
            },
        )
    assert response["actions"] == []
    for secret in ("AB-12C", "AB12C"):
        assert secret not in json.dumps([payload, fields, response])


@pytest.mark.parametrize("spelling", ["１２３４５６", "ⒶⒷⒸⒹⒺⒻ", "ABCDEF"])
def test_code_masking_preserves_unrelated_unicode(spelling: str) -> None:
    with skyvern_context.scoped(SkyvernContext()):
        canonical = skyvern_context.normalize_multi_field_totp_code(spelling, 6)
        result = agent_module._replace_multi_field_totp_code(f"password paﬀword; code ({spelling}).", canonical, _HINT)
    assert result == f"password paﬀword; code ({_HINT})."


@pytest.mark.asyncio
@pytest.mark.parametrize("hint", [_HINT, "SECOND"])
async def test_second_rejection_masks_every_delivered_hash_after_cleanup(
    monkeypatch: pytest.MonkeyPatch, hint: str
) -> None:
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now))
    step = make_step(now, task, step_id="step", status=StepStatus.completed, order=0, output=None)
    rejection = MultiFieldTotpRejection(
        hashlib.sha256(b"FIRSTA").hexdigest(), now, None, "Rejected", retry_used=True, submitted_at=now, hint_code=hint
    )
    rejection.delivered_code_hashes.add(hashlib.sha256(b"SECOND").hexdigest())
    context = SkyvernContext(task_id=task.task_id, multi_field_totp_rejections={task.task_id: rejection})
    classifier = AsyncMock(return_value={"code_rejected": True, "reason": "Rejected"})
    monkeypatch.setattr(agent_module, "get_org_aware_primary_llm_api_handler", lambda: classifier)
    monkeypatch.setattr(agent_module.LLMAPIHandlerFactory, "get_override_llm_api_handler", lambda *a, **k: classifier)
    with skyvern_context.scoped(context):
        outcome = await ForgeAgent()._maybe_retry_multi_field_totp_after_rejection(
            task,
            step,
            None,
            None,
            "The second code SECOND, SEC OND, SEC-OND, SEC - OND, SEC   OND, SEC / — OND and ＳＥＣＯＮＤ was rejected after FIRSTA.",
        )
    assert outcome == multi_field_totp_module.RetryOutcome.SECOND_REJECTION
    assert "SECOND" not in classifier.await_args.kwargs["prompt"]
    assert "FIRSTA" not in classifier.await_args.kwargs["prompt"]
    for spelling in ("SEC OND", "SEC-OND", "SEC - OND", "SEC   OND", "SEC / — OND", "ＳＥＣＯＮＤ"):
        assert spelling not in classifier.await_args.kwargs["prompt"]


@pytest.mark.asyncio
async def test_observer_installation_cancellation_removes_console_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    listeners = []
    started = asyncio.Event()

    async def bind(*args, **kwargs):
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(multi_field_totp_module.MultiFieldTotpFillObserver, "bind", bind)
    page = SimpleNamespace(
        on=lambda event, callback: listeners.append(callback),
        remove_listener=lambda event, callback: listeners.remove(callback),
    )
    pending = asyncio.create_task(multi_field_totp_module.install_multi_field_totp_fill_observer(page, [], _attempt()))
    await started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert listeners == []


def _probe_live_fill_observer(*, remount: bool | str, clear_at: int) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the input observer without a browser")
    program = (
        r"""
const install = PROBE;
globalThis.getComputedStyle = node => ({display: node.display || 'block', visibility: 'visible'});
const counts = [];
console.debug = value => counts.push(value);
const doc = {
    documentElement: {setAttribute: (name, value) => {doc.count = value;}},
    addEventListener: (event, listener) => {doc.listener = listener;},
    removeEventListener: () => {},
};
let mutation;
globalThis.MutationObserver = class {
    constructor(callback) { mutation = callback; }
    observe() {}
    disconnect() {}
};
const makeScope = () => ({isConnected: true, nodes: [], parentElement: doc, querySelectorAll() {return this.nodes;}, contains(node) {return this.nodes.includes(node);}});
let scope = makeScope();
const makeBox = value => ({ownerDocument: doc, parentElement: scope, getRootNode: () => doc,
    isConnected: true, value, getAttribute: name => ({type: 'text', maxlength: '1'})[name] ?? null});
let boxes = Array.from({length: 4}, () => makeBox(''));
scope.nodes = boxes;
doc.querySelectorAll = () => scope.nodes;
boxes.forEach((box, i) => install(box, {prefix: 'count:', reset: i === 0, replaceBoxes: i === 0, expected: 4}));
let submissions = 0;
for (let i = 0; i < 4; i++) {
    boxes[i].value = 'x';
    doc.activeElement = boxes[i];
    doc.listener({target: boxes[i], composedPath: () => [boxes[i], scope, doc]});
    if (REMOUNT) {
        const old = boxes;
        if (REMOUNT === 'container' || REMOUNT === 'hidden_aggregate') {scope.isConnected = false; scope = makeScope();}
        boxes = old.map(box => makeBox(box.value));
        old.forEach(box => {box.isConnected = false;});
        scope.nodes = [...boxes];
        if (REMOUNT === 'hidden_aggregate') {
            scope.nodes.push({...makeBox('xxxx'), getAttribute: name => ({type: 'hidden'})[name] ?? null});
            scope.nodes.push({...makeBox('x'), getAttribute: name => ({type: 'text', maxlength: '1', 'aria-hidden': 'true'})[name] ?? null});
            scope.nodes.push({...makeBox('x'), display: 'none'});
        }
        doc.activeElement = boxes[i];
        mutation?.();
    }
    if ((i + 1) % CLEAR_AT === 0) {
        if (boxes.every(box => box.value !== '')) submissions++;
        boxes.forEach(box => {box.value = '';});
        mutation?.();
    }
}
process.stdout.write(JSON.stringify({max: Number(doc.count), empty: boxes.every(box => box.value === ''), counts, submissions}));
""".replace("PROBE", multi_field_totp_module._MULTI_FIELD_TOTP_OBSERVER_JS)
        .replace("REMOUNT", json.dumps(remount))
        .replace("CLEAR_AT", str(clear_at))
    )
    result = subprocess.run([node, "-e", program], check=True, capture_output=True, text=True, timeout=10)
    return json.loads(result.stdout)


@pytest.mark.parametrize("clear_at", [2, 4])
@pytest.mark.parametrize("remount", [True, "container", "hidden_aggregate"])
def test_live_observer_counts_replaced_inputs(clear_at: int, remount: bool | str) -> None:
    observed = _probe_live_fill_observer(remount=remount, clear_at=clear_at)
    assert observed["max"] == clear_at
    assert observed["empty"]
    assert observed["submissions"] == (1 if clear_at == 4 else 0)


@pytest.mark.parametrize("change", ["counter", "ticker"])
def test_plain_rejection_counter_is_evidence_but_neighboring_timer_is_not(change: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the DOM probe without a browser")
    program = r"""
const probe = PROBE;
globalThis.getComputedStyle = () => ({visibility: 'visible', display: 'block'});
const element = {tagName: 'P', attributes: [], children: [], isConnected: true,
    childNodes: [{nodeType: 3, textContent: 'Invalid code; 3 attempts left. Resend in 30s.'}], getClientRects: () => [1]};
const before = probe(element);
element.childNodes[0].textContent = AFTER;
process.stdout.write(JSON.stringify({before, after: probe(element)}));
""".replace("PROBE", multi_field_totp_module._MULTI_FIELD_TOTP_WIDGET_FINGERPRINT_JS).replace(
        "AFTER",
        json.dumps(
            "Invalid code; 2 attempts left. Resend in 30s."
            if change == "counter"
            else "Invalid code; 3 attempts left. Resend in 29s."
        ),
    )
    output = subprocess.run([node, "-e", program], capture_output=True, text=True, check=True, timeout=10)
    snapshots = json.loads(output.stdout)
    baseline = multi_field_totp_module.MultiFieldTotpSubmissionBaseline(
        "same", "loader", snapshots["before"], frozenset()
    )
    assert multi_field_totp_module._multi_field_totp_widget_changed(baseline, snapshots["after"]) is (
        change == "counter"
    )


@pytest.mark.parametrize("spelling", ["123 456", "ABC-DEF", "ＡＢＣ：ＤＥＦ"])
@pytest.mark.parametrize("target", ["armed", "detected"])
@pytest.mark.asyncio
async def test_formatted_payload_uses_canonical_burst_and_blocks_ordinary_group_input(
    monkeypatch: pytest.MonkeyPatch, spelling: str, target: str
) -> None:
    canonical = "123456" if spelling.startswith("123") else "ABCDEF"
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), navigation_payload={"verification_code": spelling})
    task.navigation_goal = f"Enter {spelling}; preserve paﬀword."
    step = make_step(now, task, step_id="step", status=StepStatus.created, order=0, output=None)
    context = SkyvernContext(task_id=task.task_id)
    page = _box_page(6)
    monkeypatch.setattr(handler, "DomUtil", MagicMock(side_effect=AssertionError("Candidate used ordinary input")))
    with skyvern_context.scoped(context):
        payload = ForgeAgent()._build_navigation_payload(task, step=step, scraped_page=page)
        fields = agent_module._replace_multi_field_totp_prompt_code(
            task.task_id, {"navigation_goal": task.navigation_goal}
        )
        attempt = context.multi_field_totp[task.task_id]
        assert await _resolve_multi_field_totp_code(task, attempt) == canonical
        assert "paﬀword" in fields["navigation_goal"]
        target_id = attempt.box_element_ids[0]
        if target == "detected":
            page = _box_page(6)
            for index, element in enumerate(page.element_tree[0]["children"]):
                element["id"] = f"remounted-{index}"
            target_id = "remounted-0"
        for text in (spelling, canonical, f" {canonical} ", f"{canonical}\n"):
            persisted = action_for_multi_field_totp_persistence(
                InputTextAction(element_id=target_id, task_id=task.task_id, text=text)
            ).model_dump_json()
            assert json.loads(persisted)["text"] == text
            action = InputTextAction(element_id=target_id, task_id=task.task_id, text=text)
            result = await handler._handle_input_text_action(action, SimpleNamespace(), page, task, step)
            assert isinstance(result[0], ActionFailure)
        assert spelling not in json.dumps([payload, fields], ensure_ascii=False)
        assert canonical not in json.dumps([payload, fields])


@pytest.mark.parametrize("spelling", ["AB-12C", "AB12C", "ＡＢ－１２Ｃ"])
def test_candidate_metadata_is_preserved_recursively(spelling: str) -> None:
    context = SkyvernContext(task_id="task")
    with skyvern_context.scoped(context):
        assert skyvern_context.normalize_multi_field_totp_code("AB-12C", 6) is None
        metadata = {
            "tagName": "input",
            "attributes": {"aria-label": f"Code {spelling}", "placeholder": spelling},
            "text": spelling,
            "children": [{"attributes": {"title": spelling}}],
        }
        original = deepcopy(metadata)
        action = InputTextAction(
            element_id="box-0",
            task_id="task",
            text="AB12C",
            reasoning=f"Type {spelling}",
            skyvern_element_data=metadata,
        )
        persisted = action_for_multi_field_totp_persistence(action).model_dump_json()
        assert metadata == original
        assert json.loads(persisted)["skyvern_element_data"] == metadata
        assert "AB12C" in persisted


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Use SEC OND; paﬀword", "Use *; paﬀword"),
        ("Use SEC-OND or ＳＥＣＯＮＤ", "Use * or *"),
        ("Use S E.C·O:N/D", "Use *"),
        ("SEC  OND", "*"),
        ("SEC\u00a0OND SEC–OND SEC—OND", "* * *"),
        ("SEC   OND", "*"),
        ("SEC / — OND", "*"),
        ("SEC" + " " * 42 + "OND", "*"),
        ("xSECOND SECONDx", "xSECOND SECONDx"),
    ],
)
def test_gapped_code_span_boundaries(text: str, expected: str) -> None:
    spans = skyvern_context.find_multi_field_totp_code_spans(
        text, hashes={hashlib.sha256(b"SECOND").hexdigest()}, expected_digits=6, raw_forms=()
    )
    actual, cursor = [], 0
    for start, end in spans:
        actual.extend((text[cursor:start], "*"))
        cursor = end
    actual.append(text[cursor:])
    assert "".join(actual) == expected


@pytest.mark.parametrize("state", ["none", "candidate", "ledger", "attempt"])
def test_persistence_keeps_customer_fields_with_or_without_multi_box_state(state: str) -> None:
    context = SkyvernContext(task_id="task")
    actions = [
        TerminateAction(task_id="task", reasoning="The site rejected ABCDEF", output={"message": "ABC DEF"}),
        ClickAction(
            task_id="task",
            element_id="submit",
            skyvern_element_data={
                "attributes": {"aria-label": "Submit ABCDEF"},
                "children": [{"text": "ＡＢＣＤＥＦ"}],
            },
        ),
        InputTextAction(
            task_id="task",
            element_id="box-0",
            text=" ABCDEF\n",
            intention="Enter ABC-DEF",
            response="ABC / DEF",
            input_or_select_context=InputOrSelectContext(intention="Use ABCDEF"),
        ),
    ]
    with skyvern_context.scoped(context):
        if state == "candidate":
            skyvern_context.register_multi_field_totp_candidate("ABCDEF", for_multi_field=True)
        elif state == "ledger":
            context.multi_field_totp_rejections["task"] = MultiFieldTotpRejection(
                hashlib.sha256(b"ABCDEF").hexdigest(), datetime.now(UTC), None, "Rejected"
            )
        elif state == "attempt":
            context.multi_field_totp["task"] = _attempt()
            context.totp_codes["task_totp_cache"] = "ABCDEF"
        for action in actions:
            original = action.model_dump_json()
            copy = action_for_multi_field_totp_persistence(action)
            assert action.model_dump_json() == original
            assert copy.model_dump_json() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["payload", "resolver"])
@pytest.mark.parametrize("spelling", ["ABC-DEFG", "ABC-DEF"])
async def test_nested_multi_box_layout_registers_before_exact_width_admission(
    monkeypatch: pytest.MonkeyPatch, spelling: str, entry: str
) -> None:
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), navigation_payload={"verification_code": spelling})
    task.navigation_goal = f"Use {spelling} or {spelling.replace('-', '')}."
    step = make_step(now, task, step_id="step", status=StepStatus.created, order=0, output=None)
    page = _box_page(6, parent_tag="section")
    boxes = page.element_tree[0]["children"]
    page.element_tree[0]["children"] = [
        {"id": f"row-{index}", "tagName": "div", "attributes": {}, "frame": "frame-a", "children": children}
        for index, children in enumerate((boxes[:3], boxes[3:]))
    ]
    page.elements = _flatten(page.element_tree)
    context = SkyvernContext(task_id=task.task_id)
    agent = ForgeAgent()
    response = None
    if entry == "resolver":
        monkeypatch.setattr(
            agent_module, "resolve_otp_value", AsyncMock(return_value=OTPValue(value=spelling, type=OTPType.TOTP))
        )
        monkeypatch.setattr(agent_module.service_utils, "is_cua_task", AsyncMock(return_value=False))
        llm = AsyncMock(return_value={"actions": [{"text": spelling}]})
        monkeypatch.setattr(agent_module, "get_org_aware_primary_llm_api_handler", lambda: llm)
        monkeypatch.setattr(agent_module.LLMAPIHandlerFactory, "get_override_llm_api_handler", lambda *a, **k: llm)

        async def build_prompt(*args, **kwargs):
            agent._build_navigation_payload(task, step=step, scraped_page=page)
            return SimpleNamespace(
                prompt="", prompt_name="extract-action", use_caching=False, without_page_information=True
            )

        monkeypatch.setattr(agent, "_build_extract_action_prompt", build_prompt)
    with skyvern_context.scoped(context):
        if entry == "resolver":
            response = await agent.handle_potential_verification_code(
                task,
                step,
                page,
                SimpleNamespace(),
                {"place_to_enter_verification_code": True, "actions": [{"text": spelling}]},
            )
            assert spelling in context.multi_field_totp_mask_values[task.task_id]
            if spelling == "ABC-DEFG":
                assert response["actions"] == [{"text": spelling}]
        payload = agent._build_navigation_payload(task, step=step, scraped_page=page)
        goal = agent_module._replace_multi_field_totp_prompt_code(
            task.task_id, {"navigation_goal": task.navigation_goal}
        )
        assert spelling in context.multi_field_totp_mask_values[task.task_id]
        if spelling == "ABC-DEF":
            assert spelling not in json.dumps([payload, goal])
            assert spelling.replace("-", "") not in json.dumps([payload, goal])
            assert context.totp_codes[f"{task.task_id}_totp_cache"] == "ABCDEF"
            assert context.multi_field_totp[task.task_id].expected_digits == 6
        else:
            assert payload["verification_code"] == spelling
            assert goal["navigation_goal"] == task.navigation_goal
            assert f"{task.task_id}_totp_cache" not in context.totp_codes
            assert task.task_id not in context.multi_field_totp


@pytest.mark.asyncio
async def test_secret_button_fill_preserves_delivery_through_planned_terminate(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization, max_steps_per_run=10)
    task.workflow_run_id = "workflow"
    task.navigation_payload = {"credential": {"totp": "credential-token"}}
    reason = "The page shows the explicit invalid-passcode message, which requires ending the process."
    terminate = TerminateAction(task_id=task.task_id, reasoning=reason)
    output = agent_module.AgentStepOutput(
        action_results=[ActionSuccess()], actions_and_results=[(terminate, [ActionSuccess()])], errors=[]
    )
    step = make_step(now, task, step_id="step-terminate", status=StepStatus.completed, order=2, output=output)
    next_step = make_step(now, task, step_id="step-retry", status=StepStatus.created, order=3, output=None)
    snapshot = _box_page(6)
    for box in snapshot.element_tree[0]["children"]:
        box["attributes"]["style"] = "caret-color: transparent !important;"
    fresh = deepcopy(snapshot)
    for box in fresh.element_tree[0]["children"]:
        box["attributes"]["style"] = ""
    fresh.generate_scraped_page_without_screenshots = AsyncMock(return_value=fresh)
    workflow = _CredentialContext(_SEED)
    manager = SimpleNamespace(workflow_run_contexts={"workflow": workflow}, get_workflow_run_context=lambda _: workflow)
    monkeypatch.setattr(agent_module.app, "WORKFLOW_CONTEXT_MANAGER", manager)
    monkeypatch.setattr(agent_module, "_generate_multi_field_totp_hint", lambda _: _HINT)
    monkeypatch.setattr(handler, "time", SimpleNamespace(time=lambda: 60.0, perf_counter=lambda: 60.0))
    elements = _fake_group_elements(6)
    values = [""] * 6

    async def stream(code):
        values[:] = list(code)

    async def submit_click():
        values[:] = [""] * 6

    page = SimpleNamespace(url=task.url, keyboard=SimpleNamespace(type=AsyncMock(side_effect=stream)))
    submit_control = SimpleNamespace(click=AsyncMock(side_effect=submit_click))
    monkeypatch.setattr(handler, "_resolve_multi_field_totp_group_elements", AsyncMock(return_value=elements))
    monkeypatch.setattr(handler, "_apply_secret_visual_mask_if_needed", AsyncMock())
    monkeypatch.setattr(handler, "_read_multi_field_totp_values", AsyncMock(side_effect=lambda _: list(values)))
    monkeypatch.setattr(handler, "get_main_document_loader_id", AsyncMock(return_value="loader"))
    monkeypatch.setattr(agent_module, "get_main_document_loader_id", AsyncMock(return_value="loader"))
    monkeypatch.setattr(
        agent_module,
        "_refresh_multi_field_totp_group_binding",
        AsyncMock(return_value=(fresh, [f"box-{i}" for i in range(6)])),
    )
    classifier = AsyncMock(return_value={"code_rejected": True, "reason": "Invalid passcode"})
    monkeypatch.setattr(agent_module, "get_org_aware_primary_llm_api_handler", lambda: classifier)
    monkeypatch.setattr(agent_module.LLMAPIHandlerFactory, "get_override_llm_api_handler", lambda *a, **k: classifier)
    monkeypatch.setattr(agent_module, "_resolve_multi_field_totp_code", AsyncMock(return_value="483917"))
    monkeypatch.setattr(agent_module, "clear_multi_field_totp_boxes", AsyncMock(return_value=True))
    monkeypatch.setattr(agent_module, "_fill_multi_field_totp_group", AsyncMock(return_value=ActionSuccess()))
    monkeypatch.setattr(agent_module, "submit_multi_field_totp_retry", AsyncMock(return_value=True))
    agent = ForgeAgent()
    monkeypatch.setattr(agent_module.app.DATABASE.tasks, "create_step", AsyncMock(return_value=next_step))
    monkeypatch.setattr(agent, "update_step", AsyncMock(return_value=step))
    update_task = AsyncMock(return_value=task)
    monkeypatch.setattr(agent, "update_task", update_task)
    monkeypatch.setattr(agent, "_check_workflow_run_step_budget", AsyncMock(return_value=None))
    context = SkyvernContext(task_id=task.task_id)
    with skyvern_context.scoped(context):
        agent._build_navigation_payload(task, step=step, scraped_page=snapshot)
        state = context.multi_field_totp[task.task_id]
        state.valid_from, state.valid_until = 0.0, 120.0
        context.totp_codes[f"{task.task_id}_totp_cache"] = _CODE
        fill_result = await _fill_multi_field_totp_group(page, snapshot, task, state, _CODE)
        assert isinstance(fill_result, ActionSuccess) and state.fill_verified
        delivered = deepcopy(state)
        await submit_control.click()
        action_for_multi_field_totp_persistence(ClickAction(task_id=task.task_id, element_id="verify"))
        context.pop_totp_code(task.task_id)
        agent._build_navigation_payload(task, step=step, scraped_page=fresh)
        action_for_multi_field_totp_persistence(terminate)
        assert context.multi_field_totp[task.task_id] is state and state == delivered
        result = await agent.handle_completed_step(
            organization,
            task,
            step,
            page,
            browser_state=SimpleNamespace(engine_selection=None),
            scraped_page=fresh,
            complete_verification=False,
        )
    assert classifier.await_args is not None, "Verified secret delivery never reached the rejection classifier"
    assert result[2] is next_step and update_task.await_args is None
    assert context.multi_field_totp_rejections[task.task_id].submitted_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["page", "snapshot"])
async def test_retry_missing_browser_state_logs_skip(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now))
    step = make_step(now, task, step_id="step", status=StepStatus.completed, order=0, output=None)
    attempt = _attempt()
    attempt.filled_code_hash = hashlib.sha256(_CODE.encode()).hexdigest()
    attempt.filled_at, attempt.filled_url = 10.0, task.url
    context = SkyvernContext(task_id=task.task_id, multi_field_totp={task.task_id: attempt})
    with skyvern_context.scoped(context), structlog.testing.capture_logs() as logs:
        result = await ForgeAgent()._maybe_retry_multi_field_totp_after_rejection(
            task,
            step,
            None if missing == "page" else SimpleNamespace(url=task.url),
            None if missing == "snapshot" else _box_page(6),
            "Invalid code",
        )
    assert result == multi_field_totp_module.RetryOutcome.NO_RETRY
    skipped = [entry for entry in logs if entry["event"] == "Multi-field TOTP retry skipped"]
    assert len(skipped) == 1 and skipped[0]["reason"] == f"no_{missing}"
    assert (skipped[0]["task_id"], skipped[0]["workflow_run_id"], skipped[0]["step_id"]) == (
        task.task_id,
        task.workflow_run_id,
        step.step_id,
    )


@pytest.mark.parametrize(
    ("before", "after", "same"),
    [
        ("caret-color: transparent !important;", "", True),
        ("color: red; caret-color: transparent !important;", "color: red;", True),
        ("caret-color: transparent !important; display: none;", "", False),
        ("caret-color: red !important;", "", False),
        ("color: red; caret-color: transparent !important;", "color: blue;", False),
    ],
)
def test_group_identity_ignores_only_screenshot_caret_style(before: str, after: str, same: bool) -> None:
    original = _box_page(6)
    fresh = _box_page(6)
    original.element_tree[0]["children"][0]["attributes"]["style"] = before
    fresh.element_tree[0]["children"][0]["attributes"]["style"] = after
    assert (
        multi_field_totp_module.multi_field_totp_group_identity(original, 6)
        == multi_field_totp_module.multi_field_totp_group_identity(fresh, 6)
    ) is same
    assert original.element_tree[0]["children"][0]["attributes"]["style"] == before


@pytest.mark.asyncio
async def test_two_numeric_inputs_keep_the_single_field_code_path(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), navigation_payload={"verification_code": "123456"})
    task.navigation_goal = "Enter 123456 into the verification input."
    step = make_step(now, task, step_id="step", status=StepStatus.created, order=0, output=None)
    page = _box_page(2)
    for element in page.element_tree[0]["children"]:
        element["attributes"].pop("maxlength", None)
        element["attributes"]["inputmode"] = "numeric"
    actions = [{"action_type": "input_text", "element_id": "box-0", "text": "123456"}]
    context = SkyvernContext(task_id=task.task_id)
    agent = ForgeAgent()
    monkeypatch.setattr(
        agent_module, "resolve_otp_value", AsyncMock(return_value=OTPValue(value="123456", type=OTPType.TOTP))
    )
    monkeypatch.setattr(agent_module.service_utils, "is_cua_task", AsyncMock(return_value=False))
    llm = AsyncMock(return_value={"actions": actions})
    monkeypatch.setattr(agent_module, "get_org_aware_primary_llm_api_handler", lambda: llm)
    monkeypatch.setattr(agent_module.LLMAPIHandlerFactory, "get_override_llm_api_handler", lambda *a, **k: llm)
    monkeypatch.setattr(
        agent,
        "_build_extract_action_prompt",
        AsyncMock(
            return_value=SimpleNamespace(
                prompt="Enter 123456", prompt_name="extract-action", use_caching=False, without_page_information=True
            )
        ),
    )
    with skyvern_context.scoped(context):
        payload = agent._build_navigation_payload(task, step=step, scraped_page=page)
        prompt = agent_module._replace_multi_field_totp_prompt_code(
            task.task_id, {"navigation_goal": task.navigation_goal}
        )
        assert payload["verification_code"] == "123456"
        assert prompt["navigation_goal"] == task.navigation_goal
        assert task.task_id not in context.multi_field_totp
        response = await agent.handle_potential_verification_code(
            task, step, page, SimpleNamespace(), {"place_to_enter_verification_code": True, "actions": actions}
        )
        assert response["actions"] == actions
        assert task.task_id not in context.multi_field_totp
        assert (
            "123456"
            in action_for_multi_field_totp_persistence(
                InputTextAction(task_id=task.task_id, element_id="box-0", text="123456")
            ).model_dump_json()
        )


@pytest.mark.parametrize("spelling", ["SEC" + " " * 43 + "OND", "\n        ".join("ABCDEF")])
def test_oversized_code_spelling_is_rejected_and_literal_masked(spelling: str) -> None:
    context = SkyvernContext(task_id="task")
    with skyvern_context.scoped(context):
        assert skyvern_context.normalize_multi_field_totp_code(spelling, 6) is None
        prompt = agent_module._replace_multi_field_totp_prompt_code("task", {"navigation_goal": f"Use {spelling}"})
        assert spelling not in prompt["navigation_goal"]
        action = InputTextAction(task_id="task", element_id="box-0", text=spelling)
        assert action_for_multi_field_totp_persistence(action).text == spelling


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow", [False, True])
@pytest.mark.parametrize("retention_error", [False, True])
async def test_cleanup_retains_multi_box_masking_until_artifacts_finish(
    monkeypatch, workflow: bool, retention_error: bool
) -> None:
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), workflow_run_id="workflow" if workflow else None)
    step = make_step(now, task, step_id="step", status=StepStatus.completed, order=0, output=None)
    context = SkyvernContext(task_id=task.task_id)
    agent = ForgeAgent()
    retained = []
    monkeypatch.setattr(agent_module.app.DATABASE.tasks, "get_task", AsyncMock(return_value=task))
    monkeypatch.setattr(agent_module.app.BROWSER_MANAGER, "get_for_task", lambda _: None)
    monkeypatch.setattr(agent_module.analytics, "capture", MagicMock())
    monkeypatch.setattr(agent.async_operation_pool, "remove_task", AsyncMock())
    monkeypatch.setattr(agent_module.app.ARTIFACT_MANAGER, "wait_for_upload_aiotasks", AsyncMock())
    monkeypatch.setattr(agent_module.uploaded_file_service, "delete_files_attached_to_run", AsyncMock())

    async def retain(*args, **kwargs):
        assert task.task_id in context.multi_field_totp_mask_values
        retained.append(skyvern_context.mask_multi_field_totp_artifact_text("ABC DEF", replacement="*"))
        if retention_error:
            raise RuntimeError("Retention unavailable")

    monkeypatch.setattr(agent, "cleanup_browser_and_create_artifacts", retain)
    with skyvern_context.scoped(context):
        skyvern_context.normalize_multi_field_totp_code("ABC-DEF", 6)
        if retention_error and not workflow:
            with pytest.raises(RuntimeError, match="Retention unavailable"):
                await agent.clean_up_task(task, step, need_final_screenshot=False, need_call_webhook=False)
        else:
            await agent.clean_up_task(task, step, need_final_screenshot=False, need_call_webhook=False)
        if workflow:
            # Workflow-level HAR/console retention happens after its task cleanups.
            assert task.task_id in context.multi_field_totp_mask_values
        else:
            assert retained == ["*"]
            assert task.task_id not in skyvern_context.multi_field_totp_masking_task_ids()


def test_step_output_retention_is_unchanged_without_multi_box_state() -> None:
    action = TerminateAction(task_id="task", reasoning="The site rejected ABCDEF")
    output = agent_module.AgentStepOutput(actions_and_results=[(action, [ActionSuccess()])])
    with skyvern_context.scoped(SkyvernContext()):
        expected = output.model_dump_json()
    with skyvern_context.scoped(SkyvernContext(task_id="task")):
        assert output.model_dump_json() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("seam", ["planned", "periodic"])
@pytest.mark.parametrize("decision", ["first", "second", "second_negative", "second_error"])
async def test_parsed_terminate_referents_survive_persistence_and_reach_classifier(
    monkeypatch: pytest.MonkeyPatch, seam: str, decision: str
) -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization, max_steps_per_run=10)
    reason = "The user explicitly requires termination when the invalid-passcode error is visible."
    intention = "What should be done if the submitted verification passcode is reported as invalid?"
    response = "Terminate the process."
    snapshot = _box_page(6)
    snapshot.url = task.url
    snapshot.generate_scraped_page_without_screenshots = AsyncMock(return_value=snapshot)
    state = _attempt()
    state.filled_code_hash = hashlib.sha256(_CODE.encode()).hexdigest()
    state.filled_at, state.filled_url, state.filled_loader_id = 10.0, task.url, "loader"
    context = SkyvernContext(task_id=task.task_id, multi_field_totp={task.task_id: state})
    context.totp_codes[f"{task.task_id}_totp_cache"] = _CODE
    if decision != "first":
        context.multi_field_totp_rejections[task.task_id] = MultiFieldTotpRejection(
            state.filled_code_hash, now, 0, "Rejected", retry_used=True, submitted_at=now
        )
    step = make_step(now, task, step_id="step", status=StepStatus.completed, order=1, output=None)
    next_step = make_step(now, task, step_id="next", status=StepStatus.created, order=2, output=None)
    with skyvern_context.scoped(context):
        parsed = agent_module.parse_actions(
            task,
            step.step_id,
            step.order,
            snapshot,
            [
                {
                    "action_type": "TERMINATE",
                    "reasoning": reason,
                    "user_detail_query": intention,
                    "user_detail_answer": response,
                }
            ],
        )[0]
        assert isinstance(parsed, TerminateAction)
        assert (parsed.intention, parsed.response) == (intention, response)
        persisted = action_for_multi_field_totp_persistence(parsed)
        assert (persisted.intention, persisted.response) == (intention, response)
        step.output = agent_module.AgentStepOutput(
            action_results=[ActionSuccess()], actions_and_results=[(parsed, [ActionSuccess()])], errors=[]
        )
        step = type(step).model_validate_json(step.model_dump_json())
        hydrated = step.output.actions_and_results[0][0]
        assert not isinstance(hydrated, TerminateAction)
        assert hydrated.action_type == agent_module.ActionType.TERMINATE
        assert (hydrated.intention, hydrated.response) == (persisted.intention, persisted.response)
        if seam == "periodic":
            step.output = agent_module.AgentStepOutput(action_results=[], actions_and_results=[], errors=[])
    classifier = AsyncMock(return_value={"code_rejected": decision != "second_negative", "reason": "Classification"})
    if decision == "second_error":
        classifier.side_effect = TimeoutError
    monkeypatch.setattr(agent_module, "get_org_aware_primary_llm_api_handler", lambda: classifier)
    monkeypatch.setattr(agent_module.LLMAPIHandlerFactory, "get_override_llm_api_handler", lambda *a, **k: classifier)
    monkeypatch.setattr(agent_module, "get_main_document_loader_id", AsyncMock(return_value="loader"))
    monkeypatch.setattr(
        agent_module,
        "_refresh_multi_field_totp_group_binding",
        AsyncMock(return_value=(snapshot, state.box_element_ids)),
    )
    monkeypatch.setattr(agent_module, "_resolve_multi_field_totp_code", AsyncMock(return_value="483917"))
    monkeypatch.setattr(agent_module, "clear_multi_field_totp_boxes", AsyncMock(return_value=True))
    monkeypatch.setattr(agent_module, "_fill_multi_field_totp_group", AsyncMock(return_value=ActionSuccess()))
    monkeypatch.setattr(agent_module, "submit_multi_field_totp_retry", AsyncMock(return_value=True))
    agent = ForgeAgent()
    monkeypatch.setattr(agent, "get_failure_reason_for_task", AsyncMock(return_value=reason))
    monkeypatch.setattr(agent, "update_step", AsyncMock(return_value=step))
    updated_task = AsyncMock(return_value=task)
    monkeypatch.setattr(agent, "update_task", updated_task)
    monkeypatch.setattr(agent, "_check_workflow_run_step_budget", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_module.app.DATABASE.tasks, "create_step", AsyncMock(return_value=next_step))
    monkeypatch.setattr(agent, "check_user_goal_complete", AsyncMock(return_value=parsed))
    monkeypatch.setattr(agent, "_speculate_next_step_plan", AsyncMock(return_value=None))
    monkeypatch.setattr(agent, "_persist_speculative_metadata_for_discarded_plan", AsyncMock())
    monkeypatch.setattr(agent, "record_artifacts_after_action", AsyncMock())
    monkeypatch.setattr(agent_module.ActionHandler, "handle_action", AsyncMock(return_value=[ActionSuccess()]))
    with skyvern_context.scoped(context):
        if seam == "planned":
            result = await agent.handle_completed_step(
                organization,
                task,
                step,
                SimpleNamespace(url=task.url),
                browser_state=SimpleNamespace(engine_selection=None),
                scraped_page=snapshot,
                complete_verification=False,
            )
        else:
            result = await agent._handle_completed_step_with_parallel_verification(
                organization,
                task,
                step,
                SimpleNamespace(url=task.url),
                SimpleNamespace(engine_selection=None),
                snapshot,
                RunEngine.skyvern_v1,
            )
        for pending in context.pending_speculative_persist_tasks:
            await pending
    prompt = classifier.await_args.kwargs["prompt"]
    assert intention in prompt, "Parsed and persisted intention was lost at the terminate seam"
    assert response in prompt, "Parsed and persisted response was lost at the terminate seam"
    retry_context = BeautifulSoup(prompt, "html.parser").find("retry_context")
    assert (retry_context is not None) is (decision != "first")
    if retry_context is not None:
        assert "second code" in retry_context.text
        assert _CODE not in retry_context.text
    if decision == "first":
        assert result[2] is next_step and updated_task.await_args is None
        assert context.multi_field_totp_rejections[task.task_id].submitted_at is not None
    else:
        assert result[2] is None
        assert updated_task.await_args.kwargs["failure_reason"] == (
            SECOND_REJECTION_REASON if decision == "second" else reason
        )


_CONTEXT_DESTROYED_ERROR = "Execution context was destroyed, most likely because of a navigation."


class _NeverResolvingHandle:
    """Frame ElementHandle whose direct handle reads never resolve, so a regression that reads the id
    straight off the handle (``get_attribute`` re-resolving the driver's 30s ``:scope`` selector, or a
    handle-bound ``evaluate``) instead of through the common ``SkyvernFrame.evaluate`` abstraction trips
    the bounded wait."""

    async def get_attribute(self, name: str) -> str | None:
        await asyncio.Event().wait()
        raise AssertionError("get_attribute should never resolve")

    async def evaluate(self, expression: str, arg: object = None) -> object:
        await asyncio.Event().wait()
        raise AssertionError("handle.evaluate should never resolve")


class _FrameStub:
    def __init__(self, handle: _NeverResolvingHandle, parent_frame: object, *, detached: bool = False) -> None:
        self._handle = handle
        self.parent_frame = parent_frame
        self._detached = detached

    def is_detached(self) -> bool:
        return self._detached

    async def frame_element(self) -> _NeverResolvingHandle:
        return self._handle


class _EvaluateSpy:
    """Stands in for the common ``SkyvernFrame.evaluate`` abstraction and records how it was called."""

    def __init__(self, *, result: object) -> None:
        self._result = result
        self.calls: list[SimpleNamespace] = []

    async def __call__(self, *, frame: object, expression: str, arg: object = None, **kwargs: object) -> object:
        self.calls.append(SimpleNamespace(frame=frame, expression=expression, arg=arg, kwargs=kwargs))
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


def _child_frame(handle: _NeverResolvingHandle) -> _FrameStub:
    return _FrameStub(handle, parent_frame=object())


def _page_with_child(child: _FrameStub) -> SimpleNamespace:
    return SimpleNamespace(main_frame=child.parent_frame, frames=[child.parent_frame, child])


@pytest.mark.asyncio
async def test_frame_gone_returns_false_when_original_id_still_live(monkeypatch: pytest.MonkeyPatch) -> None:
    frame_id = "CD34"
    handle = _NeverResolvingHandle()
    child = _child_frame(handle)
    page = _page_with_child(child)
    spy = _EvaluateSpy(result=frame_id)
    monkeypatch.setattr(multi_field_totp_module.SkyvernFrame, "evaluate", spy)

    result = await asyncio.wait_for(_multi_field_totp_frame_gone(page, frame_id), timeout=2)

    assert result is False
    assert len(spy.calls) == 1
    # The iframe's ElementHandle lives in the PARENT frame's execution context, so the id must be
    # read there with the handle as the argument -- not from the child frame or off the handle.
    assert spy.calls[0].frame is child.parent_frame
    assert spy.calls[0].arg is handle


@pytest.mark.asyncio
async def test_frame_gone_reads_orphan_id_via_main_frame_context(monkeypatch: pytest.MonkeyPatch) -> None:
    # An orphan iframe (child attach before its parent) briefly has parent_frame=None; its handle is
    # owned by main_frame, so the read must evaluate there -- not from the child frame.
    frame_id = "CD34"
    handle = _NeverResolvingHandle()
    child = _FrameStub(handle, parent_frame=None)
    main_frame = object()
    page = SimpleNamespace(main_frame=main_frame, frames=[main_frame, child])
    spy = _EvaluateSpy(result=frame_id)
    monkeypatch.setattr(multi_field_totp_module.SkyvernFrame, "evaluate", spy)

    result = await asyncio.wait_for(_multi_field_totp_frame_gone(page, frame_id), timeout=2)

    assert result is False
    assert len(spy.calls) == 1
    assert spy.calls[0].frame is main_frame
    assert spy.calls[0].arg is handle


@pytest.mark.asyncio
async def test_frame_gone_returns_true_when_original_id_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _page_with_child(_child_frame(_NeverResolvingHandle()))
    monkeypatch.setattr(multi_field_totp_module.SkyvernFrame, "evaluate", _EvaluateSpy(result="EF56"))

    result = await asyncio.wait_for(_multi_field_totp_frame_gone(page, "CD34"), timeout=2)

    assert result is True


@pytest.mark.asyncio
async def test_frame_gone_indeterminate_when_frame_id_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _page_with_child(_child_frame(_NeverResolvingHandle()))
    monkeypatch.setattr(multi_field_totp_module.SkyvernFrame, "evaluate", _EvaluateSpy(result=None))

    result = await asyncio.wait_for(_multi_field_totp_frame_gone(page, "CD34"), timeout=2)

    assert result is None


@pytest.mark.asyncio
async def test_frame_gone_indeterminate_when_execution_context_destroyed(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _page_with_child(_child_frame(_NeverResolvingHandle()))
    monkeypatch.setattr(
        multi_field_totp_module.SkyvernFrame, "evaluate", _EvaluateSpy(result=PlaywrightError(_CONTEXT_DESTROYED_ERROR))
    )

    result = await asyncio.wait_for(_multi_field_totp_frame_gone(page, "CD34"), timeout=2)

    assert result is None


@pytest.mark.asyncio
async def test_retry_discovery_batches_control_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    controls = [object() for _ in range(4)]
    scope = SimpleNamespace(
        evaluate=AsyncMock(
            return_value=[
                {
                    "tag": "button",
                    "type": "submit",
                    "label": "Verify ABC DEF",
                    "form_matches": True,
                    "has_form": True,
                    "disabled": True,
                    "aria_disabled": False,
                    "visible": True,
                },
                {
                    "tag": "button",
                    "type": "submit",
                    "label": "Verify ABC DEF",
                    "form_matches": True,
                    "has_form": True,
                    "disabled": False,
                    "aria_disabled": False,
                    "visible": True,
                },
                {
                    "tag": "button",
                    "type": "submit",
                    "label": "Verify unrelated",
                    "form_matches": False,
                    "has_form": True,
                    "disabled": False,
                    "aria_disabled": False,
                    "visible": True,
                },
                {
                    "tag": "button",
                    "type": "button",
                    "label": "Continue",
                    "form_matches": True,
                    "has_form": True,
                    "disabled": False,
                    "aria_disabled": False,
                    "visible": True,
                },
            ]
        ),
        locator=lambda _: SimpleNamespace(nth=lambda index: controls[index]),
    )
    monkeypatch.setattr(multi_field_totp_module, "_multi_field_totp_widget_scopes", AsyncMock(return_value=[scope]))
    candidates = await multi_field_totp_module.find_multi_field_totp_submit_controls([])
    assert [candidate.locator for candidate in candidates] == [controls[1], controls[3]]
    assert candidates[0].log_fields["rank_word"] == "verify"
    assert "ABC DEF" not in str([candidate.log_fields for candidate in candidates])
    assert scope.evaluate.await_args is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
async def test_retry_submit_timing_and_verified_binding_reuse(monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    state = _attempt()
    state.fill_verified = True
    page = SimpleNamespace(url="same")
    scraped = SimpleNamespace(url="same", _document_loader_id="loader")
    submitted = False

    async def click(**kwargs):
        nonlocal submitted
        if kwargs.get("trial"):
            return
        if outcome == "cancel":
            raise asyncio.CancelledError("Do not log ABC DEF")
        if outcome == "error":
            raise PlaywrightTimeoutError("Do not log ABC DEF")
        submitted = True

    control = SimpleNamespace(count=AsyncMock(return_value=1), click=click)
    candidate = SimpleNamespace(locator=control, log_fields={"control_tag": "button", "rank_word": "verify"})
    box = SimpleNamespace(count=AsyncMock(return_value=1))
    monkeypatch.setattr(multi_field_totp_module, "_retry_box_locators", AsyncMock(return_value=[box]))
    monkeypatch.setattr(
        multi_field_totp_module, "find_multi_field_totp_submit_controls", AsyncMock(return_value=[candidate])
    )
    monkeypatch.setattr(
        multi_field_totp_module, "capture_multi_field_totp_submission_baseline", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(multi_field_totp_module, "get_main_document_loader_id", AsyncMock(return_value="loader"))
    monkeypatch.setattr(
        multi_field_totp_module,
        "_refresh_multi_field_totp_group_binding",
        AsyncMock(side_effect=AssertionError("Accepted binding was re-scraped")),
    )
    monkeypatch.setattr(
        multi_field_totp_module,
        "asyncio",
        ScopedAsyncio(sleep=AsyncMock(side_effect=AssertionError("Verified fill waited before dispatch"))),
    )
    monkeypatch.setattr(
        multi_field_totp_module,
        "multi_field_totp_submission_evidence",
        AsyncMock(side_effect=lambda *a, **kw: submitted),
    )
    with structlog.testing.capture_logs() as logs:
        if outcome == "success":
            assert await multi_field_totp_module.submit_multi_field_totp_retry(
                page, scraped, state, binding_confirmed=True, task_id="task", workflow_run_id="workflow", step_id="step"
            )
        else:
            with pytest.raises(asyncio.CancelledError if outcome == "cancel" else PlaywrightTimeoutError):
                await multi_field_totp_module.submit_multi_field_totp_retry(
                    page,
                    scraped,
                    state,
                    binding_confirmed=True,
                    task_id="task",
                    workflow_run_id="workflow",
                    step_id="step",
                )
    events = [entry for entry in logs if entry["event"] == "Multi-field TOTP retry submit timing"]
    assert len(events) == 1
    event = events[0]
    assert (event["task_id"], event["workflow_run_id"], event["step_id"]) == ("task", "workflow", "step")
    assert event["candidates_considered"] == 1
    for field in (
        "pre_evidence_ms",
        "rebind_ms",
        "locators_ms",
        "discovery_ms",
        "baseline_ms",
        "dispatch_ms",
        "post_evidence_ms",
        "total_ms",
    ):
        assert isinstance(event[field], (float, int)) and event[field] >= 0
    assert event["rebind_ms"] == 0
    assert "ABC DEF" not in str(logs)


@pytest.mark.asyncio
async def test_resolver_uses_rejection_width_after_attempt_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now))
    step = make_step(now, task, step_id="step", status=StepStatus.created, order=0, output=None)
    rejection = MultiFieldTotpRejection(hashlib.sha256(b"ABCDEF").hexdigest(), now, None, "Rejected", expected_digits=6)
    context = SkyvernContext(task_id=task.task_id, multi_field_totp_rejections={task.task_id: rejection})

    async def resolve(*args, **kwargs):
        return otp_service._exclude_rejected_otp(
            OTPValue(value="ABC - DEF", type=OTPType.TOTP),
            kwargs["rejected_code_hash"],
            kwargs.get("multi_field_expected_digits"),
            task_id=task.task_id,
        )

    monkeypatch.setattr(agent_module, "resolve_otp_value", resolve)
    agent = ForgeAgent()
    build = AsyncMock(side_effect=AssertionError("Rejected resend reached the planner"))
    monkeypatch.setattr(agent, "_build_extract_action_prompt", build)
    with skyvern_context.scoped(context):
        result = await agent.handle_potential_verification_code(
            task,
            step,
            _box_page(6),
            SimpleNamespace(),
            {"place_to_enter_verification_code": True, "actions": [{"text": "ABC - DEF"}]},
        )
    assert result["actions"] == []
    assert not build.await_args_list


@pytest.mark.asyncio
@pytest.mark.parametrize("from_seed", [False, True])
async def test_verification_code_reprompt_without_context(monkeypatch: pytest.MonkeyPatch, from_seed: bool) -> None:
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now))
    step = make_step(now, task, step_id="step", status=StepStatus.created, order=0, output=None)
    monkeypatch.setattr(skyvern_context, "current", lambda: None)
    monkeypatch.setattr(
        agent_module,
        "resolve_otp_value",
        AsyncMock(return_value=OTPValue(value=_CODE, type=OTPType.TOTP, from_credential_seed=from_seed)),
    )
    agent = ForgeAgent()
    monkeypatch.setattr(
        agent,
        "_build_extract_action_prompt",
        AsyncMock(
            return_value=SimpleNamespace(
                prompt="enter code", use_caching=False, prompt_name="extract-action", without_page_information=True
            )
        ),
    )
    monkeypatch.setattr(agent_module.service_utils, "is_cua_task", AsyncMock(return_value=False))
    response = {"actions": [{"action_type": "INPUT_TEXT", "text": _CODE}]}
    monkeypatch.setattr(
        agent_module.LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        lambda *a, **k: AsyncMock(return_value=response),
    )
    assert (
        await agent.handle_potential_verification_code(
            task, step, _box_page(6), SimpleNamespace(), {"place_to_enter_verification_code": True}
        )
        == response
    )
    assert skyvern_context.current() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["external", "secret"])
@pytest.mark.parametrize(
    "entry",
    [
        "plan",
        "generate",
        "generate_terminal",
        "verification",
        "verification_after_teardown",
        "resolver",
        "handler",
        "handler_unarmed",
        "handler_after_teardown",
        "fill",
        "submit",
    ],
)
async def test_submitted_retry_budget_blocks_further_code_entry(
    monkeypatch: pytest.MonkeyPatch, source: str, entry: str
) -> None:
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), workflow_run_id="workflow", totp_identifier="identifier")
    step = make_step(now, task, step_id="step-budget", status=StepStatus.created, order=0, output=None)
    attempt = _attempt()
    attempt.code_source = source
    retry_code = "483917"
    attempt.filled_code_hash = hashlib.sha256(retry_code.encode()).hexdigest()
    attempt.filled_at = now.timestamp()
    attempt.filled_url = "same"
    attempt.filled_loader_id = "loader"
    attempt.fill_verified = True
    attempt.observed_max_filled = attempt.expected_digits
    attempt.valid_from = now.timestamp()
    attempt.valid_until = now.timestamp() + 30
    rejection = MultiFieldTotpRejection(
        hashlib.sha256(_CODE.encode()).hexdigest(),
        now,
        None,
        "Rejected",
        retry_used=True,
        submitted_at=now,
        hint_code=attempt.hint_code,
    )
    rejection.delivered_code_hashes.add(hashlib.sha256(retry_code.encode()).hexdigest())
    context = SkyvernContext(
        task_id=task.task_id,
        workflow_run_id=task.workflow_run_id,
        step_id=step.step_id,
        multi_field_totp={task.task_id: attempt},
        multi_field_totp_rejections={task.task_id: rejection},
        totp_codes={f"{task.task_id}_totp_cache": retry_code, f"{task.task_id}_secret": _SEED},
    )
    snapshot = _box_page(6)
    snapshot.url = "same"
    snapshot.id_to_element_hash = {}
    snapshot.id_to_element_dict = {element["id"]: element for element in snapshot.elements}
    page = SimpleNamespace(url="same", keyboard=SimpleNamespace(type=AsyncMock()))
    resolver = AsyncMock(side_effect=AssertionError("A submitted retry must not fetch another code"))
    monkeypatch.setattr(agent_module, "resolve_otp_value", resolver)
    monkeypatch.setattr(
        handler,
        "parse_totp_config",
        MagicMock(side_effect=AssertionError("A submitted retry must not generate another code")),
    )
    dom = MagicMock(side_effect=AssertionError("An exhausted retry reached the browser"))
    monkeypatch.setattr(handler, "DomUtil", dom)
    monkeypatch.setattr(handler, "get_main_document_loader_id", AsyncMock(return_value="loader"))
    monkeypatch.setattr(
        multi_field_totp_module,
        "_retry_box_locators",
        AsyncMock(side_effect=AssertionError("An exhausted retry reached submission")),
    )
    monkeypatch.setattr(agent_module.app.WORKFLOW_CONTEXT_MANAGER, "has_workflow_run_context", lambda _: False)
    actions = [
        {"action_type": "input_text", "element_id": element_id, "text": attempt.hint_code[index]}
        for index, element_id in enumerate(attempt.box_element_ids)
    ] + [{"action_type": "click", "element_id": "verify"}]
    response = {"place_to_enter_verification_code": True, "should_enter_verification_code": True, "actions": actions}
    if entry == "generate_terminal":
        actions.append({"action_type": "terminate", "reasoning": "The second code was rejected."})
    with skyvern_context.scoped(context), structlog.testing.capture_logs() as logs:
        if entry in {"verification_after_teardown", "handler_after_teardown"}:
            context.clear_multi_field_totp_state(task.task_id)
        if entry in {"generate", "generate_terminal"}:
            snapshot.check_pdf_viewer_embed = lambda: None
            snapshot.check_pdf_iframe = AsyncMock(return_value=None)
            monkeypatch.setattr(
                agent_module.LLMAPIHandlerFactory,
                "get_override_llm_api_handler",
                lambda *a, **kw: AsyncMock(side_effect=AssertionError("Must reuse the existing plan")),
            )
            generated, _, _ = await ForgeAgent()._generate_step_actions(
                task=task,
                step=step,
                browser_state=SimpleNamespace(),
                engine=RunEngine.skyvern_v1,
                scraped_page=snapshot,
                detailed_agent_step_output=agent_module.DetailedAgentStepOutput(
                    scraped_page=None,
                    extract_action_prompt=None,
                    llm_response=None,
                    actions=None,
                    action_results=None,
                    actions_and_results=None,
                ),
                injected_actions=None,
                is_extraction_task=False,
                prefetched_summary_task=None,
                cua_response=None,
                llm_caller=None,
                extract_action_prompt="",
                prompt_name="extract-actions",
                use_caching=False,
                without_page_information=True,
                json_response=response,
                reuse_speculative_llm_response=True,
                speculative_llm_metadata=None,
                context=context,
            )
            if entry == "generate_terminal":
                assert len(generated) == 1 and isinstance(generated[0], TerminateAction)
                assert generated[0].reasoning == "The second code was rejected."
            else:
                assert generated == []
        elif entry == "plan":
            returned, parsed = await ForgeAgent().handle_potential_OTP_actions(
                task, step, snapshot, SimpleNamespace(), response
            )
            assert returned["actions"] == [] and parsed == []
        elif entry.startswith("verification"):
            returned = await ForgeAgent().handle_potential_verification_code(
                task, step, snapshot, SimpleNamespace(), response
            )
            assert returned["actions"] == []
        elif entry == "submit":
            assert not await multi_field_totp_module.submit_multi_field_totp_retry(
                page, snapshot, attempt, task_id=task.task_id, step_id=step.step_id
            )
        else:
            if entry == "resolver":
                result = await _resolve_multi_field_totp_code(task, attempt)
            elif entry.startswith("handler"):
                action = InputTextAction(
                    element_id=attempt.box_element_ids[0],
                    text=attempt.hint_code[0],
                    task_id=task.task_id,
                    totp_timing_info={
                        "is_totp_sequence": True,
                        "action_index": 0,
                        "box_element_ids": attempt.box_element_ids,
                    }
                    if entry == "handler"
                    else None,
                )
                result = (await handler._handle_input_text_action(action, page, snapshot, task, step))[0]
                assert redact_action_for_log(action)["text"] != action.text
            else:
                result = await _fill_multi_field_totp_group(page, snapshot, task, attempt, retry_code)
            assert isinstance(result, ActionFailure)
            assert result.skip_remaining_actions and not result.success
            assert "retry budget" in result.exception_message.lower()
    assert not page.keyboard.type.await_args_list and not dom.call_args_list and not resolver.await_args_list
    events = [item for item in logs if item.get("reason") == "retry_budget_exhausted"]
    assert len(events) == 1
    assert (events[0]["task_id"], events[0]["workflow_run_id"], events[0]["step_id"]) == (
        task.task_id,
        task.workflow_run_id,
        step.step_id,
    )
    assert (
        _CODE not in str(events) and retry_code not in str(events) and rejection.rejected_code_hash not in str(events)
    )
    assert context.multi_field_totp_rejections[task.task_id] is rejection and rejection.submitted_at == now


@pytest.mark.asyncio
@pytest.mark.parametrize("single_field_credential", [False, True])
async def test_submitted_retry_allows_later_work(
    monkeypatch: pytest.MonkeyPatch, single_field_credential: bool
) -> None:
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), workflow_run_id="workflow", totp_identifier="identifier")
    step = make_step(now, task, step_id="step-after-login", status=StepStatus.created, order=1, output=None)
    attempt = _attempt()
    attempt.code_source = "external"
    rejection = MultiFieldTotpRejection(
        hashlib.sha256(b"123456").hexdigest(), now, None, "Rejected", retry_used=True, submitted_at=now
    )
    context = SkyvernContext(
        task_id=task.task_id,
        multi_field_totp={task.task_id: attempt},
        multi_field_totp_rejections={task.task_id: rejection},
        totp_codes={f"{task.task_id}_totp_cache": _CODE},
    )
    workflow = WorkflowRunContext("title", "workflow-id", "permanent-id", "workflow", None)
    workflow.parameters["credentials"] = CredentialParameter.model_construct()
    workflow.values["credentials"] = {"totp": "credential_totp"}
    workflow.secrets["credential_totp"] = BitwardenConstants.TOTP
    workflow.secrets["credential_totp_value"] = _SEED
    monkeypatch.setattr(
        agent_module.app,
        "WORKFLOW_CONTEXT_MANAGER",
        SimpleNamespace(has_workflow_run_context=lambda _: True, get_workflow_run_context=lambda _: workflow),
    )
    monkeypatch.setattr(
        agent_module, "resolve_otp_value", AsyncMock(side_effect=AssertionError("No verification re-plan needed"))
    )
    actions = (
        [
            {"action_type": "input_text", "element_id": "single-otp", "text": "credential_totp"},
            {"action_type": "click", "element_id": "confirm"},
        ]
        if single_field_credential
        else [
            {"action_type": "click", "element_id": "menu"},
            {"action_type": "input_text", "element_id": "search", "text": "monthly report"},
            {"action_type": "extract", "reasoning": "Read the report"},
            {"action_type": "goto_url", "url": "https://example.test/reports"},
        ]
    )
    plan = {
        "place_to_enter_verification_code": single_field_credential,
        "should_enter_verification_code": single_field_credential,
        "actions": actions,
    }
    original = deepcopy(plan)
    agent = ForgeAgent()
    with skyvern_context.scoped(context), structlog.testing.capture_logs() as logs:
        if not single_field_credential:
            returned = await agent.handle_potential_verification_code(task, step, _box_page(1), SimpleNamespace(), plan)
            assert returned is plan and returned == original
        returned, parsed = await agent.handle_potential_OTP_actions(task, step, _box_page(1), SimpleNamespace(), plan)
    assert returned is plan and returned == original and parsed == []
    assert not any(item.get("reason") == "retry_budget_exhausted" for item in logs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "gate", ["no_attempt", "no_delivery", "no_page", "no_snapshot", "url_changed", "loader_changed", "spent"]
)
async def test_retry_structural_rejection_preserves_speculative_plan(
    monkeypatch: pytest.MonkeyPatch, gate: str
) -> None:
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now))
    step = make_step(now, task, step_id="step", status=StepStatus.completed, order=0, output=None)
    attempt = _attempt()
    attempt.filled_code_hash = hashlib.sha256(_CODE.encode()).hexdigest()
    attempt.filled_at = now.timestamp()
    attempt.filled_url = "same"
    attempt.filled_loader_id = "loader"
    context = SkyvernContext(multi_field_totp={task.task_id: attempt})
    page, snapshot = SimpleNamespace(url="same"), _box_page(6)
    if gate == "no_attempt":
        context.multi_field_totp.clear()
    elif gate == "no_delivery":
        attempt.filled_code_hash = None
    elif gate == "no_page":
        page = None
    elif gate == "no_snapshot":
        snapshot = None
    elif gate == "url_changed":
        page.url = "different"
    elif gate == "spent":
        context.multi_field_totp_rejections[task.task_id] = MultiFieldTotpRejection(
            hashlib.sha256(_CODE.encode()).hexdigest(), now, None, "Rejected", retry_used=True
        )
    monkeypatch.setattr(
        agent_module,
        "get_main_document_loader_id",
        AsyncMock(return_value="different" if gate == "loader_changed" else "loader"),
    )
    finish = asyncio.Event()

    async def speculate():
        await finish.wait()
        return {"cost": 1, "tokens": 2}

    pending = asyncio.create_task(speculate())
    try:
        with skyvern_context.scoped(context):
            outcome = await ForgeAgent()._maybe_retry_multi_field_totp_after_rejection(
                task,
                step,
                page,
                snapshot,
                TerminateAction(reasoning="Account locked").reasoning,
                speculative_task=pending,
            )
        assert outcome == multi_field_totp_module.RetryOutcome.NO_RETRY
        assert not pending.cancelled() and not pending.cancelling()
        finish.set()
        assert await pending == {"cost": 1, "tokens": 2}
    finally:
        finish.set()
        await asyncio.gather(pending, return_exceptions=True)


def test_multi_field_totp_hashes_are_excluded_from_repr() -> None:
    attempt = _attempt()
    attempt.filled_code_hash = hashlib.sha256(_CODE.encode()).hexdigest()
    attempt.filled_group_identity = "group-hash"
    rejection = MultiFieldTotpRejection(attempt.filled_code_hash, datetime.now(UTC), None, "Rejected")
    retry_hash = hashlib.sha256(b"ABCDEF").hexdigest()
    rejection.delivered_code_hashes.add(retry_hash)
    assert all(
        value not in repr(attempt) + repr(rejection) for value in (attempt.filled_code_hash, retry_hash, "group-hash")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("retention_fails", [False, True])
async def test_workflow_teardown_clears_multi_box_state_after_retention(
    monkeypatch: pytest.MonkeyPatch, retention_fails: bool
) -> None:
    task_ids = ["parent-task-1", "parent-task-2", "child-task"]
    context = SkyvernContext(workflow_run_id="parent-run")
    context.multi_field_totp["unrelated-task"] = _attempt()
    for task_id in task_ids:
        context.multi_field_totp[task_id] = _attempt()
        context.multi_field_totp_rejections[task_id] = MultiFieldTotpRejection(
            hashlib.sha256(_CODE.encode()).hexdigest(), datetime.now(UTC), None, "Rejected"
        )
        context.totp_codes[f"{task_id}_totp_cache"] = _CODE
        context.multi_field_totp_mask_values[task_id] = {_CODE}

    async def retain(_task_ids):
        assert set(task_ids) <= skyvern_context.multi_field_totp_masking_task_ids()
        if retention_fails:
            raise RuntimeError("retention failed")

    service = workflow_service.WorkflowService()
    monkeypatch.setattr(workflow_service, "is_retry_eligible_run", AsyncMock(return_value=False))
    monkeypatch.setattr(workflow_service.analytics, "capture", MagicMock())
    monkeypatch.setattr(workflow_service.app.DATABASE.workflow_run_attempts, "get_attempts", AsyncMock(return_value=[]))
    monkeypatch.setattr(service, "_drain_failure_evidence_captures", AsyncMock())
    monkeypatch.setattr(workflow_service.app.AGENT_FUNCTION, "on_workflow_run_terminal", AsyncMock())
    monkeypatch.setattr(
        workflow_service.app.ARTIFACT_MANAGER, "wait_for_upload_aiotasks", AsyncMock(side_effect=retain)
    )
    monkeypatch.setattr(workflow_service.app.STORAGE, "save_downloaded_files", AsyncMock())
    monkeypatch.setattr(workflow_service.uploaded_file_service, "delete_files_attached_to_run", AsyncMock())
    monkeypatch.setattr(workflow_service.app.WORKFLOW_CONTEXT_MANAGER, "remove_workflow_run_context", MagicMock())
    workflow_run = SimpleNamespace(workflow_run_id="parent-run", organization_id="org", status="completed")
    cleanup = workflow_service.WorkflowBrowserCleanupResult(
        browser_state=None,
        tasks=[],
        all_workflow_task_ids=task_ids,
        child_workflow_run_ids=["child-run"],
        close_browser_on_completion=True,
    )
    with skyvern_context.scoped(context):
        try:
            await service.clean_up_workflow(
                workflow=SimpleNamespace(),
                workflow_run=workflow_run,
                browser_cleanup_result=cleanup,
                need_call_webhook=False,
                schedule_credential_fallback_retry=False,
            )
        except RuntimeError as exc:
            assert retention_fails and str(exc) == "retention failed"
        assert skyvern_context.multi_field_totp_masking_task_ids() == {"unrelated-task"}
    assert all(task_id not in context.multi_field_totp_rejections for task_id in task_ids)
    assert not context.totp_codes


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["handler", "retry_seam"])
@pytest.mark.parametrize("auto_submit", [False, True])
async def test_verified_burst_observation_controls_retry_dispatch(
    monkeypatch: pytest.MonkeyPatch, entry: str, auto_submit: bool
) -> None:
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), totp_identifier="test@example.test")
    step = make_step(now, task, step_id="step-observe", status=StepStatus.completed, order=0, output=None)
    state = _attempt()
    state.code_source = "external"
    state.filled_code_hash = hashlib.sha256(_CODE.encode()).hexdigest()
    state.filled_at = now.timestamp()
    state.filled_url = task.url
    state.filled_loader_id = "loader"
    context = SkyvernContext(task_id=task.task_id, multi_field_totp={task.task_id: state})
    snapshot = _box_page(6)
    snapshot.url, snapshot._document_loader_id = task.url, "loader"
    snapshot.generate_scraped_page_without_screenshots = AsyncMock(return_value=snapshot)
    elements = _fake_group_elements(6)
    values = [""] * 6
    clock = SimpleNamespace(now=0.0)
    dispatches = []
    fill_results = []

    async def sleep(delay):
        clock.now += delay

    async def stream(code):
        values[:] = list(code)

    async def explicit_submit(*args, **kwargs):
        if not kwargs.get("trial"):
            dispatches.append(clock.now)
            page.url = "https://example.test/accepted"

    async def fingerprint(*args, **kwargs):
        return {"root": "accepted-feedback" if auto_submit and clock.now >= 0.2 else "pending-feedback"}

    boxes = [element.get_locator() for element in elements]
    for index, box in enumerate(boxes):
        box.input_value = AsyncMock(side_effect=lambda index=index, **_: values[index])
        box.press = AsyncMock(side_effect=explicit_submit)
    page = SimpleNamespace(url=task.url, keyboard=SimpleNamespace(type=AsyncMock(side_effect=stream)))
    widget = SimpleNamespace(evaluate=AsyncMock(side_effect=fingerprint))
    control = SimpleNamespace(count=AsyncMock(return_value=1), click=AsyncMock(side_effect=explicit_submit))
    monkeypatch.setattr(handler, "asyncio", ScopedAsyncio(sleep=sleep))
    monkeypatch.setattr(multi_field_totp_module, "asyncio", ScopedAsyncio(sleep=sleep))
    for module in (handler, agent_module, multi_field_totp_module):
        monkeypatch.setattr(module, "get_main_document_loader_id", AsyncMock(return_value="loader"))
    monkeypatch.setattr(handler, "_resolve_multi_field_totp_group_elements", AsyncMock(return_value=elements))
    monkeypatch.setattr(handler, "_read_multi_field_totp_values", AsyncMock(side_effect=lambda _: list(values)))
    monkeypatch.setattr(handler, "_apply_secret_visual_mask_if_needed", AsyncMock())
    monkeypatch.setattr(multi_field_totp_module, "_retry_box_locators", AsyncMock(return_value=boxes))
    monkeypatch.setattr(multi_field_totp_module, "_multi_field_totp_widget_scopes", AsyncMock(return_value=[widget]))
    monkeypatch.setattr(
        multi_field_totp_module,
        "find_multi_field_totp_submit_controls",
        AsyncMock(return_value=[_submit_candidate(control)]),
    )
    code = "483917"

    async def fill(*args):
        result = await _fill_multi_field_totp_group(*args)
        fill_results.append(result)
        assert clock.now <= 0.6
        return result

    with skyvern_context.scoped(context):
        if entry == "handler":
            result = await fill(page, snapshot, task, state, code)
        else:
            classifier = AsyncMock(return_value={"code_rejected": True, "reason": "Rejected"})
            monkeypatch.setattr(agent_module, "get_org_aware_primary_llm_api_handler", lambda: classifier)
            monkeypatch.setattr(
                agent_module.LLMAPIHandlerFactory, "get_override_llm_api_handler", lambda *a, **k: classifier
            )
            monkeypatch.setattr(
                agent_module,
                "_refresh_multi_field_totp_group_binding",
                AsyncMock(return_value=(snapshot, state.box_element_ids)),
            )
            monkeypatch.setattr(
                agent_module, "poll_otp_value", AsyncMock(return_value=OTPValue(value=code, type=OTPType.TOTP))
            )
            monkeypatch.setattr(agent_module, "clear_multi_field_totp_boxes", AsyncMock(return_value=True))
            monkeypatch.setattr(agent_module, "_fill_multi_field_totp_group", fill)
            outcome = await ForgeAgent()._maybe_retry_multi_field_totp_after_rejection(
                task, step, page, snapshot, "The submitted one-time code was rejected."
            )
            assert outcome == multi_field_totp_module.RetryOutcome.RETRIED
            rejection = context.multi_field_totp_rejections[task.task_id]
            assert rejection.retry_used and rejection.submitted_at is not None
            assert len(dispatches) == (0 if auto_submit else 1)
            result = fill_results[0]
    assert isinstance(result, ActionSuccess) and state.fill_verified
    assert result.data.get("totp_submission_observed", False) is auto_submit
    if auto_submit:
        assert result.data == {"totp_group_filled": True, "verified": True, "totp_submission_observed": True}
        assert not control.click.await_args_list and not boxes[-1].press.await_args_list
    else:
        assert result.data == {"totp_group_filled": True}
    assert not result.skip_remaining_actions
    assert values == list(code)
    assert all(not element.input_fill.await_args_list for element in elements)
