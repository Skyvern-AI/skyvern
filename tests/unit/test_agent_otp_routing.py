"""Regression tests for Agent OTP source routing.

Covers the post-first-plan skip seam: after the first planning pass produces a plan,
``handle_potential_OTP_actions`` may skip the polling verification re-plan only when the retained
first-pass actions are an existing multi-field consecutive single-digit sequence AND the runtime
already holds the ``totp_codes[f"{task_id}_secret"]`` stash the per-digit execution path types. This
is the sole runtime-consumable shape on this v1 two-pass seam. A ``get_verification_code`` action
must re-plan (it is not runtime-materialized on this path); a literal digit string must re-plan; a
fabricated placeholder must re-plan; a raw or wrapped provider marker input
(``BW_TOTP``/``OP_TOTP``/``AZ_TOTP``) must re-plan; an ordinary action dict carrying a ``totp`` key
must re-plan; a multi-field sequence without the runtime stash must re-plan; payload OTP must still
win; and magic-link framing must survive into the first prompt. The new gate never selects or reads
a credential candidate — the agent no longer imports ``has_credential_totp_candidate``. Also covers
that handle_potential_verification_code delegates to resolve_otp_value without a pre-resolver DB
roundtrip.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.agent import (
    ForgeAgent,
    PromptBuildResult,
    _model_is_abandoning_verification,
)
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.forge.sdk.services.bitwarden import BitwardenConstants
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.parameter import CredentialParameter
from skyvern.schemas.run_enums import RunEngine
from skyvern.services import otp_service
from skyvern.services.otp_service import OTPValue

_VALID_TOTP_SEED = "JBSWY3DPEHPK3PXP"


def _make_task(
    *,
    totp_verification_url: str | None = "https://example.com/webhook",
    totp_identifier: str | None = "user@example.com",
    navigation_payload: object = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        task_id="tsk_test",
        organization_id="o_test",
        workflow_run_id="wr_test",
        workflow_permanent_id="wpid_test",
        totp_verification_url=totp_verification_url,
        totp_identifier=totp_identifier,
        navigation_payload=navigation_payload,
        url="https://example.com",
        navigation_goal="log in",
        llm_key=None,
        workflow_system_prompt=None,
    )


class _FakeWorkflowRunContext:
    def __init__(self, values: dict[str, dict[str, str]], secrets: dict[str, str]) -> None:
        self.values = values
        self.secrets = secrets

    def totp_secret_value_key(self, totp_secret_id: str) -> str:
        return f"{totp_secret_id}_value"

    def get_original_secret_value_or_none(self, key: str) -> str | None:
        return self.secrets.get(key)


def _patch_workflow_context(monkeypatch: pytest.MonkeyPatch, fake: _FakeWorkflowRunContext) -> None:
    monkeypatch.setattr(
        otp_service,
        "app",
        SimpleNamespace(
            WORKFLOW_CONTEXT_MANAGER=SimpleNamespace(
                get_workflow_run_context=lambda _wr_id: fake,
                has_workflow_run_context=lambda _wr_id: True,
            ),
        ),
    )


def _usable_credential_context() -> _FakeWorkflowRunContext:
    return _FakeWorkflowRunContext(
        values={"credentials": {"username": "u", "password": "p", "totp": "tot"}},
        secrets={"tot_value": _VALID_TOTP_SEED},
    )


def _real_credential_context(
    *, seed: str = "otpauth://totp/Test?secret=" + _VALID_TOTP_SEED, placeholder: str = "cred_totp"
) -> WorkflowRunContext:
    context = WorkflowRunContext("title", "wid", "wpid", "wr_test", None)
    context.parameters["credentials"] = CredentialParameter.model_construct()
    context.values["credentials"] = {"username": "u", "password": "p", "totp": placeholder}
    context.secrets[placeholder] = BitwardenConstants.TOTP
    context.secrets[f"{placeholder}_value"] = seed
    return context


def _otp_json_response(actions: list, **overrides: object) -> dict:
    """First-plan LLM response on a verification page. Defaults request the verification-code
    branch (place=True, should_enter=True) so the skip predicate is what decides re-plan-vs-skip."""
    return {
        "place_to_enter_verification_code": True,
        "should_enter_verification_code": True,
        "should_verify_by_magic_link": False,
        "actions": actions,
        **overrides,
    }


async def _run_otp_actions(
    monkeypatch: pytest.MonkeyPatch,
    task: SimpleNamespace,
    json_response: dict,
) -> tuple[AsyncMock, MagicMock, tuple]:
    """Drive the real handle_potential_OTP_actions post-plan seam. Returns (hpvc mock, parse mock,
    result). hpvc awaited ⇒ the polling verification re-plan fired; not awaited ⇒ the first plan was
    kept and ``result`` is what the seam returned."""
    step = MagicMock()
    step.step_id = "stp_test"
    step.order = 0
    scraped_page = MagicMock()
    browser_state = MagicMock()

    hpvc = AsyncMock(return_value={"actions": [{"action_type": "INPUT_TEXT", "text": "999999"}]})
    monkeypatch.setattr(ForgeAgent, "handle_potential_verification_code", hpvc)
    parse_mock = MagicMock(return_value=[object()])
    monkeypatch.setattr("skyvern.forge.agent.parse_actions", parse_mock)
    monkeypatch.setattr("skyvern.forge.agent.stamp_parsed_actions", MagicMock())

    agent = ForgeAgent.__new__(ForgeAgent)
    result = await agent.handle_potential_OTP_actions(task, step, scraped_page, browser_state, json_response)
    return hpvc, parse_mock, result


_FABRICATED_PLACEHOLDER_INPUT = [{"action_type": "INPUT_TEXT", "id": "AAAA", "text": "placeholder_FAKE_totp"}]
_PROVIDER_MARKER_INPUT = [{"action_type": "INPUT_TEXT", "id": "AAAA", "text": "OP_TOTP"}]
_GET_VERIFICATION_CODE = [{"action_type": "get_verification_code", "reasoning": "fetch code"}]
_MULTI_FIELD = [{"action_type": "INPUT_TEXT", "id": f"F{i}", "text": str(i)} for i in range(1, 7)]
_SAME_FORM_MULTI_FIELD = [
    *[{"action_type": "INPUT_TEXT", "id": f"F{i}", "text": str(i)} for i in range(1, 7)],
    {"action_type": "CLICK", "id": "BBBB", "reasoning": "submit the code"},
]
_LEADING_ACTION_MULTI_FIELD = [
    {"action_type": "CLICK", "id": "BBBB", "reasoning": "focus the code field"},
    *[{"action_type": "INPUT_TEXT", "id": f"F{i}", "text": str(i)} for i in range(1, 7)],
]
_SAME_FORM_GET_VERIFICATION_CODE = [
    {"action_type": "CLICK", "id": "BBBB", "reasoning": "focus the code field"},
    {"action_type": "get_verification_code", "reasoning": "fetch code"},
]
_WRAPPED_PROVIDER_MARKER_INPUT = [{"action_type": "INPUT_TEXT", "id": "AAAA", "text": "prefix_OP_TOTP_suffix"}]
_LITERAL_INPUT = [{"action_type": "INPUT_TEXT", "id": "AAAA", "text": "123456"}]
_ORDINARY_DICT_TOTP = [{"action_type": "INPUT_TEXT", "id": "AAAA", "text": "hello", "totp": "placeholder_CGft_totp"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("active_key", ["credentials", None])
async def test_single_field_registered_credential_placeholder_skips_and_preserves_click(
    monkeypatch: pytest.MonkeyPatch, active_key: str | None
) -> None:
    context = _real_credential_context()
    manager = SimpleNamespace(
        has_workflow_run_context=lambda _id: True,
        get_workflow_run_context=lambda _id: context,
    )
    monkeypatch.setattr(otp_service.app, "WORKFLOW_CONTEXT_MANAGER", manager)
    monkeypatch.setattr("skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER", manager)
    task = _make_task()
    actions = [{"action_type": "INPUT_TEXT", "id": "AAAA", "text": "cred_totp"}, {"action_type": "CLICK", "id": "BBBB"}]
    response = _otp_json_response(actions)
    with skyvern_context.scoped(SkyvernContext(task_id=task.task_id, active_credential_parameter_key=active_key)):
        hpvc, _parse, result = await _run_otp_actions(monkeypatch, task, response)
    hpvc.assert_not_awaited()
    assert result[0] is response and result[0]["actions"] is actions and result[1] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        ("literal", [{"action_type": "INPUT_TEXT", "text": "123456"}], "credentials"),
        ("raw_marker", [{"action_type": "INPUT_TEXT", "text": "BW_TOTP"}], "credentials"),
        ("wrapped_marker", [{"action_type": "INPUT_TEXT", "text": "x_BW_TOTP_x"}], "credentials"),
        ("fabricated", [{"action_type": "INPUT_TEXT", "text": "placeholder_FAKE_totp"}], "credentials"),
        ("wrapped_registered", [{"action_type": "INPUT_TEXT", "text": "x_cred_totp"}], "credentials"),
        ("password", [{"action_type": "INPUT_TEXT", "text": "password_totp"}], "credentials"),
        ("foreign", [{"action_type": "INPUT_TEXT", "text": "other_totp"}], "credentials"),
        ("missing_seed", [{"action_type": "INPUT_TEXT", "text": "cred_totp"}], "credentials"),
        ("unparseable", [{"action_type": "INPUT_TEXT", "text": "cred_totp"}], "credentials"),
        (
            "leading_click",
            [{"action_type": "CLICK"}, {"action_type": "INPUT_TEXT", "text": "cred_totp"}],
            "credentials",
        ),
        (
            "two_inputs",
            [{"action_type": "INPUT_TEXT", "text": "cred_totp"}, {"action_type": "INPUT_TEXT", "text": "x"}],
            "credentials",
        ),
    ],
)
async def test_single_field_negative_matrix_replans(monkeypatch: pytest.MonkeyPatch, case: tuple) -> None:
    name, actions, active = case
    context = _real_credential_context(seed="bad" if name == "unparseable" else _VALID_TOTP_SEED)
    if name == "missing_seed":
        context.secrets.pop("cred_totp_value")
    if name == "foreign":
        context.values["other"] = {"totp": "other_totp"}
        context.parameters["other"] = CredentialParameter.model_construct()
        context.secrets["other_totp"] = BitwardenConstants.TOTP
        context.secrets["other_totp_value"] = "otpauth://totp/Other?secret=" + _VALID_TOTP_SEED
    if name == "password":
        context.secrets["password_totp"] = "password-secret"
        context.values["credentials"]["password"] = "password_totp"
    manager = SimpleNamespace(has_workflow_run_context=lambda _id: True, get_workflow_run_context=lambda _id: context)
    monkeypatch.setattr(otp_service.app, "WORKFLOW_CONTEXT_MANAGER", manager)
    monkeypatch.setattr("skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER", manager)
    task = _make_task()
    with skyvern_context.scoped(SkyvernContext(task_id=task.task_id, active_credential_parameter_key=active)):
        hpvc, _parse, _result = await _run_otp_actions(monkeypatch, task, _otp_json_response(actions))
    hpvc.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_actions",
    [
        pytest.param(_PROVIDER_MARKER_INPUT, id="provider_marker_replans"),
        pytest.param(_GET_VERIFICATION_CODE, id="get_verification_code_replans"),
        pytest.param(_SAME_FORM_GET_VERIFICATION_CODE, id="same_form_click_plus_get_verification_code_replans"),
        pytest.param(_LITERAL_INPUT, id="literal_digits_replans"),
        pytest.param(_FABRICATED_PLACEHOLDER_INPUT, id="fabricated_placeholder_replans"),
        pytest.param(_ORDINARY_DICT_TOTP, id="ordinary_dict_totp_key_replans"),
        pytest.param(_WRAPPED_PROVIDER_MARKER_INPUT, id="wrapped_provider_marker_replans"),
    ],
)
async def test_non_multi_field_shapes_always_replan(
    monkeypatch: pytest.MonkeyPatch,
    first_actions: list,
) -> None:
    """The sole runtime-consumable shape on this v1 two-pass seam is an existing multi-field
    single-digit sequence backed by the runtime secret stash. Every other first-pass shape must keep
    the polling re-plan — even with a usable credential registered and a runtime stash present, so a
    predicate that keyed on secret presence would wrongly skip.

    A ``get_verification_code`` action (and a same-form click preceding it) is not runtime-materialized
    on this path and must re-plan; a literal digit string, a fabricated placeholder, an ordinary action
    dict carrying a ``totp`` key, and a raw or wrapped provider marker input must all re-plan.
    """
    _patch_workflow_context(monkeypatch, _usable_credential_context())
    task = _make_task()

    ctx = SkyvernContext(
        task_id=task.task_id,
        active_credential_parameter_key="credentials",
        totp_codes={f"{task.task_id}_secret": _VALID_TOTP_SEED},
    )
    skyvern_context.set(ctx)
    try:
        hpvc, _parse_mock, _result = await _run_otp_actions(monkeypatch, task, _otp_json_response(first_actions))
    finally:
        skyvern_context.reset()

    hpvc.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actions_override",
    [
        pytest.param({}, id="actions_missing"),
        pytest.param({"actions": None}, id="actions_none"),
        pytest.param({"actions": "123456"}, id="actions_string"),
        pytest.param({"actions": {"action_type": "INPUT_TEXT"}}, id="actions_dict"),
    ],
)
async def test_missing_or_malformed_actions_fail_open_to_replan(
    monkeypatch: pytest.MonkeyPatch,
    actions_override: dict,
) -> None:
    """A first plan whose ``actions`` is absent or not a list must not blow up the skip gate: the
    optimization inspects the plan only when ``actions`` is a real list, otherwise it falls open to
    the polling verification re-plan (which rebuilds the payload). Even with the runtime secret stash
    present, a non-list payload cannot be a consumable multi-field shape."""
    _patch_workflow_context(monkeypatch, _usable_credential_context())
    task = _make_task()
    json_response = {
        "place_to_enter_verification_code": True,
        "should_enter_verification_code": True,
        "should_verify_by_magic_link": False,
        **actions_override,
    }

    ctx = SkyvernContext(
        task_id=task.task_id,
        active_credential_parameter_key="credentials",
        totp_codes={f"{task.task_id}_secret": _VALID_TOTP_SEED},
    )
    skyvern_context.set(ctx)
    try:
        hpvc, _parse_mock, _result = await _run_otp_actions(monkeypatch, task, json_response)
    finally:
        skyvern_context.reset()

    hpvc.assert_awaited_once()


def test_agent_no_longer_imports_credential_candidate_selector() -> None:
    """The new gate never selects or reads a credential candidate; the agent module must not carry the
    ``has_credential_totp_candidate`` import that the earlier gate depended on."""
    import skyvern.forge.agent as agent_module

    assert not hasattr(agent_module, "has_credential_totp_candidate")


@pytest.mark.asyncio
async def test_same_form_multi_field_preserves_exact_first_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """A same-form ``[six consecutive single-digit INPUT_TEXT, CLICK submit]`` plan backed by the
    runtime secret stash is runtime-consumable: the single-digit run begins at action-list index 0, so
    the per-digit execution path materializes it, and the model-requested trailing submit click is
    preserved. The seam keeps the first plan and returns the exact original action list untouched, with
    no re-plan."""
    _patch_workflow_context(monkeypatch, _usable_credential_context())
    task = _make_task()
    json_response = _otp_json_response(_SAME_FORM_MULTI_FIELD)
    original_actions = json_response["actions"]

    ctx = SkyvernContext(
        task_id=task.task_id,
        active_credential_parameter_key="credentials",
        totp_codes={f"{task.task_id}_secret": _VALID_TOTP_SEED},
    )
    skyvern_context.set(ctx)
    try:
        hpvc, _parse_mock, result = await _run_otp_actions(monkeypatch, task, json_response)
    finally:
        skyvern_context.reset()

    hpvc.assert_not_awaited()
    returned_json, returned_actions = result
    assert returned_json is json_response
    assert returned_json["actions"] is original_actions
    assert returned_json["actions"] == _SAME_FORM_MULTI_FIELD
    assert returned_actions == []


@pytest.mark.asyncio
async def test_leading_action_before_multi_field_replans(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``[CLICK, six consecutive single-digit INPUT_TEXT]`` plan is NOT runtime-consumable even with
    the runtime secret stash present: the leading action pushes the first digit to absolute
    ``action_index == 1``, so ``_handle_multi_field_totp_sequence`` never seeds the cache (it generates
    only at index 0) and every digit fails with a cache miss. The skip must fail open to the polling
    verification re-plan. RED on the prior shape check that matched a single-digit run at any offset."""
    _patch_workflow_context(monkeypatch, _usable_credential_context())
    task = _make_task()

    ctx = SkyvernContext(
        task_id=task.task_id,
        active_credential_parameter_key="credentials",
        totp_codes={f"{task.task_id}_secret": _VALID_TOTP_SEED},
    )
    skyvern_context.set(ctx)
    try:
        hpvc, _parse_mock, _result = await _run_otp_actions(
            monkeypatch, task, _otp_json_response(_LEADING_ACTION_MULTI_FIELD)
        )
    finally:
        skyvern_context.reset()

    hpvc.assert_awaited_once()


@pytest.mark.asyncio
async def test_multi_field_skips_only_with_runtime_secret_stash(monkeypatch: pytest.MonkeyPatch) -> None:
    """A multi-field single-digit plan is only runtime-consumable when the runtime already holds the
    code the per-digit execution path types — the ``totp_codes[f"{task_id}_secret"]`` stash the
    existing multi-field preparation reads. With the stash present the skip fires."""
    _patch_workflow_context(monkeypatch, _usable_credential_context())
    task = _make_task()

    ctx = SkyvernContext(
        task_id=task.task_id,
        active_credential_parameter_key="credentials",
        totp_codes={f"{task.task_id}_secret": _VALID_TOTP_SEED},
    )
    skyvern_context.set(ctx)
    try:
        hpvc, _parse_mock, _result = await _run_otp_actions(monkeypatch, task, _otp_json_response(_MULTI_FIELD))
    finally:
        skyvern_context.reset()

    hpvc.assert_not_awaited()


@pytest.mark.asyncio
async def test_multi_field_replans_without_runtime_secret_stash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same credential candidate and same multi-field single-digit plan, but no runtime stash: the
    per-digit execution path cannot materialize the code, so the polling verification re-plan must
    still fire. RED on the shape-only predicate that skipped on digit count alone."""
    _patch_workflow_context(monkeypatch, _usable_credential_context())
    task = _make_task()

    ctx = SkyvernContext(task_id=task.task_id, active_credential_parameter_key="credentials")
    skyvern_context.set(ctx)
    try:
        hpvc, _parse_mock, _result = await _run_otp_actions(monkeypatch, task, _otp_json_response(_MULTI_FIELD))
    finally:
        skyvern_context.reset()

    hpvc.assert_awaited_once()


@pytest.mark.asyncio
async def test_post_plan_skip_blocked_by_payload_otp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Payload OTP is highest precedence and resolved independently, so the multi-field skip must
    never run ahead of it: even a multi-field plan backed by the runtime secret stash (which would
    otherwise skip) must fall through to the re-plan when the navigation payload carries an OTP."""
    _patch_workflow_context(monkeypatch, _usable_credential_context())
    task = _make_task(navigation_payload={"otp_code": "654321"})

    ctx = SkyvernContext(
        task_id=task.task_id,
        active_credential_parameter_key="credentials",
        totp_codes={f"{task.task_id}_secret": _VALID_TOTP_SEED},
    )
    skyvern_context.set(ctx)
    try:
        hpvc, _parse_mock, _result = await _run_otp_actions(monkeypatch, task, _otp_json_response(_MULTI_FIELD))
    finally:
        skyvern_context.reset()

    hpvc.assert_awaited_once()


@pytest.mark.asyncio
async def test_single_field_credential_shortcut_does_not_override_payload_otp(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_workflow_context(monkeypatch, _usable_credential_context())
    task = _make_task(navigation_payload={"otp_code": "654321"})
    hpvc, _parse, _result = await _run_otp_actions(
        monkeypatch, task, _otp_json_response([{"action_type": "INPUT_TEXT", "text": "cred_totp"}])
    )
    hpvc.assert_awaited_once()


@pytest.mark.asyncio
async def test_single_field_credential_shortcut_does_not_override_magic_link(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_workflow_context(monkeypatch, _usable_credential_context())
    task = _make_task()
    step, page, browser = MagicMock(), MagicMock(), MagicMock()
    magic = AsyncMock(return_value=["magic"])
    monkeypatch.setattr(ForgeAgent, "handle_potential_magic_link", magic)
    agent = ForgeAgent.__new__(ForgeAgent)
    response = _otp_json_response(
        [{"action_type": "INPUT_TEXT", "text": "cred_totp"}],
        should_verify_by_magic_link=True,
        should_enter_verification_code=False,
        place_to_enter_verification_code=False,
    )
    actions = await agent.handle_potential_OTP_actions(task, step, page, browser, response)
    magic.assert_awaited_once()
    assert actions[1] == ["magic"]


@pytest.mark.asyncio
async def test_single_field_routing_is_planning_only(monkeypatch: pytest.MonkeyPatch) -> None:
    context = _real_credential_context()
    manager = SimpleNamespace(has_workflow_run_context=lambda _id: True, get_workflow_run_context=lambda _id: context)
    monkeypatch.setattr(otp_service.app, "WORKFLOW_CONTEXT_MANAGER", manager)
    monkeypatch.setattr("skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER", manager)
    monkeypatch.setattr(
        "skyvern.forge.sdk.services.credentials.generate_totp_code", MagicMock(side_effect=AssertionError)
    )
    task = _make_task()
    hpvc, _parse, result = await _run_otp_actions(
        monkeypatch,
        task,
        _otp_json_response([{"action_type": "INPUT_TEXT", "text": "cred_totp"}, {"action_type": "CLICK"}]),
    )
    hpvc.assert_not_awaited()
    assert result[1] == []


@pytest.mark.asyncio
async def test_post_plan_polling_only_replans(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ordinary run-context (no runtime secret stash) keeps the two-pass verification re-plan for a
    multi-field single-digit plan; the new gate never selects a credential candidate to override it."""
    _patch_workflow_context(
        monkeypatch,
        _FakeWorkflowRunContext(values={"credentials": {"username": "u", "password": "p"}}, secrets={}),
    )
    task = _make_task()

    with skyvern_context.scoped(SkyvernContext(task_id=task.task_id, active_credential_parameter_key=None)):
        hpvc, _parse_mock, _result = await _run_otp_actions(monkeypatch, task, _otp_json_response(_MULTI_FIELD))

    hpvc.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_workflow_run_context_falls_through_to_replan(monkeypatch: pytest.MonkeyPatch) -> None:
    """The optional credential optimization must not run before the established re-plan when the
    task is detached from a workflow run."""
    task = _make_task()
    task.workflow_run_id = None
    ctx = SkyvernContext(task_id=task.task_id, active_credential_parameter_key=None)
    with skyvern_context.scoped(ctx):
        hpvc, _parse_mock, _result = await _run_otp_actions(monkeypatch, task, _otp_json_response(_LITERAL_INPUT))
    hpvc.assert_awaited_once()


@pytest.mark.asyncio
async def test_first_pass_preserves_magic_link_framing_with_usable_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Magic-link framing must survive into the first extract-action prompt even when a usable
    credential TOTP and polling are both configured. The first-pass verification_code_check must be
    the base ``bool(totp_verification_url or totp_identifier)`` — not suppressed by credential
    usability — otherwise the model is never asked about magic-link and the branch is stripped
    (extract-action.j2). RED on head: the pre-plan gate returns False for a usable credential.
    """
    _patch_workflow_context(monkeypatch, _usable_credential_context())
    task = _make_task()

    step = MagicMock()
    step.step_id = "stp_test"
    step.order = 0
    step.retry_index = 0
    browser_state = MagicMock()
    scraped_page = MagicMock()
    scraped_page.elements = []

    captured: dict[str, object] = {}

    async def fake_build(
        self_agent: object,
        _task: object,
        _step: object,
        _browser_state: object,
        _scraped_page: object,
        *,
        verification_code_check: bool,
        expire_verification_code: bool,
    ) -> PromptBuildResult:
        captured["verification_code_check"] = verification_code_check
        return PromptBuildResult(prompt="p", use_caching=False, prompt_name="n", without_page_information=False)

    monkeypatch.setattr(ForgeAgent, "_build_extract_action_prompt", fake_build)

    agent = ForgeAgent.__new__(ForgeAgent)
    ctx = SkyvernContext(
        task_id=task.task_id,
        active_credential_parameter_key="credentials",
        next_step_pre_scraped_data={"step_id": step.step_id, "scraped_page": scraped_page, "timestamp": None},
    )
    skyvern_context.set(ctx)
    try:
        await agent.build_and_record_step_prompt(
            task, step, browser_state, RunEngine.skyvern_v1, persist_artifacts=False
        )
    finally:
        skyvern_context.reset()

    assert captured["verification_code_check"] is True


@pytest.mark.asyncio
async def test_handle_potential_verification_code_uses_resolver_without_db_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _make_task(navigation_payload={"otp_code": "654321"})
    step = MagicMock()
    scraped_page = MagicMock()
    browser_state = MagicMock()
    json_response = {
        "place_to_enter_verification_code": True,
        "should_enter_verification_code": True,
    }

    resolver = AsyncMock(return_value=None)
    db_get = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.resolve_otp_value", resolver)
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.workflow_runs.get_workflow_run", db_get)

    agent = ForgeAgent.__new__(ForgeAgent)
    await agent.handle_potential_verification_code(task, step, scraped_page, browser_state, json_response)

    resolver.assert_awaited_once_with(task, expected_otp_type=OTPType.TOTP)
    db_get.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_potential_verification_code_resolves_with_should_enter_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real-sink regression (calls the real sink, not a mock): with a TOTP source configured and
    place_to_enter_verification_code=True, the sink must resolve and re-plan even when
    should_enter_verification_code=False. Fails if the old inner ``(place and should_enter)`` gate is
    restored — proving the guard removal is load-bearing, not mock theater."""
    task = _make_task()
    step = MagicMock()
    scraped_page = MagicMock()
    browser_state = MagicMock()
    json_response = {
        "place_to_enter_verification_code": True,
        "should_enter_verification_code": False,
    }

    resolved_code = OTPValue(value="123456", type=OTPType.TOTP)
    resolver = AsyncMock(return_value=resolved_code)
    poll = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.resolve_otp_value", resolver)
    monkeypatch.setattr("skyvern.forge.agent.poll_otp_value", poll)

    rebuilt = AsyncMock(
        return_value=PromptBuildResult(
            prompt="prompt",
            use_caching=False,
            prompt_name="prompt_name",
            without_page_information=False,
        )
    )
    monkeypatch.setattr(ForgeAgent, "_build_extract_action_prompt", rebuilt)
    monkeypatch.setattr("skyvern.forge.agent.service_utils.is_cua_task", AsyncMock(return_value=False))

    rescrape = AsyncMock(return_value={"actions": [{"action_type": "INPUT_TEXT", "text": "123456"}]})
    monkeypatch.setattr(
        "skyvern.forge.agent.LLMAPIHandlerFactory.get_override_llm_api_handler",
        lambda *args, **kwargs: rescrape,
    )

    agent = ForgeAgent.__new__(ForgeAgent)
    agent.async_operation_pool = MagicMock()

    skyvern_context.set(SkyvernContext(task_id=task.task_id))
    try:
        result = await agent.handle_potential_verification_code(task, step, scraped_page, browser_state, json_response)
    finally:
        skyvern_context.reset()

    resolver.assert_awaited_once_with(task, expected_otp_type=OTPType.TOTP)
    rescrape.assert_awaited_once()
    assert result == {"actions": [{"action_type": "INPUT_TEXT", "text": "123456"}]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "give_up_actions",
    [
        pytest.param(
            [
                {
                    "action_type": "TERMINATE",
                    "reasoning": "OTP prompt remains; terminate per timeout policy.",
                    "user_detail_answer": "OTP_TIMEOUT",
                }
            ],
            id="terminate",
        ),
        pytest.param(
            [{"action_type": "WAIT", "reasoning": "Waiting for the verification code to arrive."}],
            id="lone_wait",
        ),
        pytest.param(
            [
                {"action_type": "WAIT", "reasoning": "Waiting for the verification code to arrive."},
                {
                    "action_type": "TERMINATE",
                    "reasoning": "OTP prompt remains; terminate per timeout policy.",
                    "user_detail_answer": "OTP_TIMEOUT",
                },
            ],
            id="wait_plus_terminate",
        ),
    ],
)
async def test_handle_potential_OTP_actions_resolves_before_model_give_up(
    monkeypatch: pytest.MonkeyPatch,
    give_up_actions: list,
) -> None:
    """A model give-up on the verification page (place_to_enter_verification_code=True,
    should_enter_verification_code=False, TOTP source configured) must route into the resolver
    instead of being honored — for both a TERMINATE and a lone WAIT — so the 15-minute poll budget
    is reachable by construction."""
    task = _make_task()
    step = MagicMock()
    step.step_id = "stp_test"
    step.order = 0
    scraped_page = MagicMock()
    browser_state = MagicMock()
    json_response = {
        "place_to_enter_verification_code": True,
        "should_enter_verification_code": False,
        "should_verify_by_magic_link": False,
        "actions": give_up_actions,
    }

    resolved_response = {"actions": [{"action_type": "INPUT_TEXT", "text": "123456"}]}
    hpvc = AsyncMock(return_value=resolved_response)
    monkeypatch.setattr(ForgeAgent, "handle_potential_verification_code", hpvc)
    parsed_sentinel = [object()]
    parse_mock = MagicMock(return_value=parsed_sentinel)
    monkeypatch.setattr("skyvern.forge.agent.parse_actions", parse_mock)
    monkeypatch.setattr("skyvern.forge.agent.stamp_parsed_actions", MagicMock())

    agent = ForgeAgent.__new__(ForgeAgent)
    _returned_response, actions = await agent.handle_potential_OTP_actions(
        task, step, scraped_page, browser_state, json_response
    )

    hpvc.assert_awaited_once()
    assert actions == parsed_sentinel
    # The resolver's rebuilt response is what gets parsed, not the model's give-up action.
    assert parse_mock.call_args.args[4] == resolved_response["actions"]


@pytest.mark.parametrize(
    "actions",
    [
        pytest.param([{"action_type": "CLICK", "id": "AAAA"}], id="lone_click"),
        # Canonical parse_actions keeps a mixed TERMINATE+CLICK batch, so forcing the resolver would
        # suppress the productive CLICK. Only a PURE give-up may route into the resolver.
        pytest.param(
            [{"action_type": "TERMINATE", "reasoning": "x"}, {"action_type": "CLICK", "id": "AAAA"}],
            id="terminate_plus_click",
        ),
        pytest.param(
            [{"action_type": "WAIT", "reasoning": "x"}, {"action_type": "CLICK", "id": "AAAA"}], id="wait_plus_click"
        ),
    ],
)
@pytest.mark.asyncio
async def test_handle_potential_OTP_actions_skips_resolver_for_non_pure_giveup(
    monkeypatch: pytest.MonkeyPatch,
    actions: list,
) -> None:
    """The guardrail must fire only on a PURE give-up. A productive action alone, or mixed in with a
    TERMINATE/WAIT (place_to_enter_verification_code=True, should_enter_verification_code=False), must
    not force a premature poll — it falls through to normal action parsing."""
    task = _make_task()
    step = MagicMock()
    step.step_id = "stp_test"
    step.order = 0
    scraped_page = MagicMock()
    browser_state = MagicMock()
    json_response = {
        "place_to_enter_verification_code": True,
        "should_enter_verification_code": False,
        "should_verify_by_magic_link": False,
        "actions": actions,
    }

    hpvc = AsyncMock()
    monkeypatch.setattr(ForgeAgent, "handle_potential_verification_code", hpvc)

    agent = ForgeAgent.__new__(ForgeAgent)
    _returned_response, returned_actions = await agent.handle_potential_OTP_actions(
        task, step, scraped_page, browser_state, json_response
    )

    hpvc.assert_not_awaited()
    assert returned_actions == []


@pytest.mark.parametrize(
    "actions, expected",
    [
        pytest.param([{"action_type": "TERMINATE", "reasoning": "x"}], True, id="pure_terminate"),
        pytest.param(
            [{"action_type": "terminate", "reasoning": "x"}, {"action_type": "TERMINATE", "reasoning": "y"}],
            True,
            id="multi_terminate",
        ),
        pytest.param([{"action_type": "WAIT", "reasoning": "x"}], True, id="lone_wait"),
        pytest.param([{"action_type": "WAIT"}, {"action_type": "WAIT"}], True, id="multi_wait"),
        pytest.param(
            [{"action_type": "TERMINATE"}, {"action_type": "CLICK", "id": "A"}], False, id="terminate_plus_click"
        ),
        pytest.param([{"action_type": "WAIT"}, {"action_type": "CLICK", "id": "A"}], False, id="wait_plus_click"),
        # _execute_step_actions drops WAIT from a mixed batch, so WAIT+TERMINATE really executes as a
        # lone TERMINATE and must classify as abandonment (order-independent).
        pytest.param(
            [{"action_type": "WAIT"}, {"action_type": "TERMINATE", "reasoning": "x"}], True, id="wait_plus_terminate"
        ),
        pytest.param(
            [{"action_type": "TERMINATE", "reasoning": "x"}, {"action_type": "WAIT"}], True, id="terminate_plus_wait"
        ),
        pytest.param([{"action_type": "CLICK", "id": "A"}], False, id="lone_click"),
        pytest.param([], False, id="empty"),
    ],
)
def test_model_is_abandoning_verification_pure_batch_only(actions: list, expected: bool) -> None:
    """Abandonment is judged on the actions that will actually execute. A pure TERMINATE or pure WAIT
    batch is abandonment; because _execute_step_actions drops WAIT from a mixed batch, WAIT+TERMINATE
    (either order) also executes as a lone TERMINATE and is abandonment. A TERMINATE or WAIT mixed
    with a productive action (which survives execution) is not abandonment."""
    assert _model_is_abandoning_verification({"actions": actions}) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "give_up_actions",
    [
        pytest.param([{"action_type": "TERMINATE", "reasoning": "give up"}], id="terminate"),
        pytest.param([{"action_type": "WAIT", "reasoning": "waiting"}], id="lone_wait"),
    ],
)
async def test_handle_potential_OTP_actions_magic_link_wins_over_give_up_fallback(
    monkeypatch: pytest.MonkeyPatch,
    give_up_actions: list,
) -> None:
    """When magic-link verification is requested (should_verify_by_magic_link=True) but
    should_enter_verification_code=False, a model give-up must NOT be hijacked by the
    verification-code resolver — the established magic-link path runs instead."""
    task = _make_task()
    step = MagicMock()
    step.step_id = "stp_test"
    step.order = 0
    scraped_page = MagicMock()
    browser_state = MagicMock()
    json_response = {
        "place_to_enter_verification_code": True,
        "should_enter_verification_code": False,
        "should_verify_by_magic_link": True,
        "actions": give_up_actions,
    }

    # Give the (wrong) verification-code branch harmless stubs so, if it is taken, the test fails on
    # the explicit assertion below rather than on an incidental downstream error.
    hpvc = AsyncMock(return_value={"actions": []})
    magic = AsyncMock(return_value=["magic_link_action"])
    monkeypatch.setattr(ForgeAgent, "handle_potential_verification_code", hpvc)
    monkeypatch.setattr(ForgeAgent, "handle_potential_magic_link", magic)
    monkeypatch.setattr("skyvern.forge.agent.parse_actions", MagicMock(return_value=[]))
    monkeypatch.setattr("skyvern.forge.agent.stamp_parsed_actions", MagicMock())

    agent = ForgeAgent.__new__(ForgeAgent)
    returned_response, actions = await agent.handle_potential_OTP_actions(
        task, step, scraped_page, browser_state, json_response
    )

    hpvc.assert_not_awaited()
    magic.assert_awaited_once()
    assert actions == ["magic_link_action"]
    assert returned_response == json_response
