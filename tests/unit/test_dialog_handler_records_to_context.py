"""Tests for dialog-handler recording into SkyvernContext."""

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.webeye import dialog_handler


def _make_dialog(dialog_type: str, message: str, default_value: str = "") -> MagicMock:
    dialog = MagicMock()
    dialog.type = dialog_type
    dialog.message = message
    dialog.default_value = default_value
    dialog.accept = AsyncMock()
    dialog.dismiss = AsyncMock()
    return dialog


@pytest.fixture
def isolated_context() -> Generator[SkyvernContext, None, None]:
    ctx = SkyvernContext(
        organization_id="o_test",
        task_id="tsk_test",
        workflow_run_id="wr_test",
    )
    skyvern_context.set(ctx)
    try:
        yield ctx
    finally:
        skyvern_context.reset()


@pytest.mark.asyncio
async def test_alert_records_into_context_and_auto_accepts(isolated_context: SkyvernContext) -> None:
    dialog = _make_dialog("alert", "The value of '47' is invalid.")

    await dialog_handler._handle_dialog(dialog)

    dialog.accept.assert_awaited_once()
    assert isolated_context.recent_dialog_messages == [
        {"type": "alert", "message": "The value of '47' is invalid.", "count": 1}
    ]


@pytest.mark.asyncio
async def test_repeated_alerts_dedupe_with_count(isolated_context: SkyvernContext) -> None:
    dialog = _make_dialog("alert", "phone invalid")

    for _ in range(5):
        await dialog_handler._handle_dialog(dialog)

    assert len(isolated_context.recent_dialog_messages) == 1
    assert isolated_context.recent_dialog_messages[0]["count"] == 5


@pytest.mark.asyncio
async def test_no_context_does_not_raise() -> None:
    skyvern_context.reset()
    dialog = _make_dialog("alert", "something")
    await dialog_handler._handle_dialog(dialog)
    dialog.accept.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_failure_does_not_break_dialog_acceptance(
    isolated_context: SkyvernContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_kw: object) -> None:
        raise RuntimeError("simulated record failure")

    monkeypatch.setattr(SkyvernContext, "record_dialog_message", boom)
    dialog = _make_dialog("alert", "anything")

    await dialog_handler._handle_dialog(dialog)

    dialog.accept.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_alert_dialogs_are_not_recorded(
    isolated_context: SkyvernContext,
) -> None:
    isolated_context.navigation_goal = "test"

    await dialog_handler._handle_dialog(_make_dialog("confirm", "Are you sure?"))
    await dialog_handler._handle_dialog(_make_dialog("prompt", "Enter your name:"))
    await dialog_handler._handle_dialog(_make_dialog("beforeunload", "Changes may not be saved."))

    assert isolated_context.recent_dialog_messages == []


class TestAcceptancePreflight:
    """SKY-12875: every confirm/prompt ACCEPT routes through one choke point that runs the
    observe-only acceptance preflight with the registration-time page. The decision is discarded —
    the response must be identical whether the policy observes or not."""

    @pytest.fixture
    def preflight_spy(self, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
        calls: list[dict] = []

        def spy(page: object, *, dialog_type: str, response: str, site: str) -> None:
            calls.append({"page": page, "dialog_type": dialog_type, "response": response, "site": site})

        monkeypatch.setattr(dialog_handler, "preflight_dialog_response", spy)
        return calls

    @pytest.mark.asyncio
    async def test_auto_accepted_confirm_is_preflighted_with_the_registered_page(
        self, isolated_context: SkyvernContext, preflight_spy: list[dict]
    ) -> None:
        page = MagicMock()
        dialog = _make_dialog("confirm", "Are you sure?")
        await dialog_handler._handle_dialog(dialog, page=page)
        dialog.accept.assert_awaited_once()
        assert preflight_spy == [
            {"page": page, "dialog_type": "confirm", "response": "accept", "site": "dialog_handler"}
        ]

    @pytest.mark.asyncio
    async def test_llm_accepted_prompt_is_preflighted(
        self, isolated_context: SkyvernContext, preflight_spy: list[dict], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        isolated_context.navigation_goal = "goal"
        monkeypatch.setattr(dialog_handler.prompt_engine, "load_prompt", lambda *a, **k: "prompt")
        monkeypatch.setattr(
            dialog_handler.app,
            "SECONDARY_LLM_API_HANDLER",
            AsyncMock(return_value={"action": "accept", "prompt_text": "Jamie"}),
        )
        dialog = _make_dialog("prompt", "Enter your name:")
        await dialog_handler._handle_dialog(dialog, page=MagicMock())
        dialog.accept.assert_awaited_once_with("Jamie")
        assert [call["dialog_type"] for call in preflight_spy] == ["prompt"]

    @pytest.mark.asyncio
    async def test_a_dismissal_is_preflighted_too(
        self, isolated_context: SkyvernContext, preflight_spy: list[dict], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        isolated_context.navigation_goal = "goal"
        monkeypatch.setattr(dialog_handler.prompt_engine, "load_prompt", lambda *a, **k: "prompt")
        monkeypatch.setattr(
            dialog_handler.app,
            "SECONDARY_LLM_API_HANDLER",
            AsyncMock(return_value={"action": "dismiss"}),
        )
        dialog = _make_dialog("confirm", "Are you sure?")
        await dialog_handler._handle_dialog(dialog, page=MagicMock())
        dialog.dismiss.assert_awaited_once()
        dialog.accept.assert_not_awaited()
        # THE CHOICE IS THE CAPABILITY: a page can branch on dismiss (probed in real Chromium
        # firing an exfil POST specifically on it), so dismissal is evaluated like acceptance.
        assert [(call["dialog_type"], call["response"]) for call in preflight_spy] == [("confirm", "dismiss")]

    @pytest.mark.asyncio
    async def test_the_llm_error_fallback_accept_is_still_preflighted(
        self, isolated_context: SkyvernContext, preflight_spy: list[dict], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        isolated_context.navigation_goal = "goal"
        monkeypatch.setattr(dialog_handler.prompt_engine, "load_prompt", lambda *a, **k: "prompt")
        monkeypatch.setattr(
            dialog_handler.app, "SECONDARY_LLM_API_HANDLER", AsyncMock(side_effect=RuntimeError("llm down"))
        )
        dialog = _make_dialog("confirm", "Proceed?")
        await dialog_handler._handle_dialog(dialog, page=MagicMock())
        dialog.accept.assert_awaited_once()
        assert [call["dialog_type"] for call in preflight_spy] == ["confirm"]

    @pytest.mark.asyncio
    async def test_alert_accepts_are_not_preflighted_but_beforeunload_accepts_are(
        self, isolated_context: SkyvernContext, preflight_spy: list[dict]
    ) -> None:
        # Alert has one possible response — no choice, no capability. Accepting a beforeunload
        # commits a pending navigation, so it routes through the choke point.
        alert = _make_dialog("alert", "message")
        await dialog_handler._handle_dialog(alert, page=MagicMock())
        alert.accept.assert_awaited_once()
        assert preflight_spy == []

        beforeunload = _make_dialog("beforeunload", "message")
        await dialog_handler._handle_dialog(beforeunload, page=MagicMock())
        beforeunload.accept.assert_awaited_once()
        assert [(call["dialog_type"], call["response"]) for call in preflight_spy] == [("beforeunload", "accept")]

    @pytest.mark.asyncio
    async def test_the_response_is_identical_with_the_real_preflight_observing(
        self, isolated_context: SkyvernContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Parity with the REAL function, not a spy, on an ENROLLED run whose decision is a real
        # DENIED (unenrolled parity is vacuous: NOT_ENROLLED cannot tempt a handler into
        # branching). The accept calls must match exactly between disabled and observe modes, or
        # observe mode changed a dialog's fate.
        from skyvern.config import settings
        from skyvern.forge.sdk.browser_action_policy import PolicyOutcome, declare_policy
        from skyvern.forge.sdk.browser_action_preflight import preflight_dialog_response

        isolated_context.browser_action_policy = declare_policy(owner_id="wr_test", origin_urls=["https://example.com"])
        monkeypatch.setattr(settings, "BROWSER_ACTION_POLICY_MODE", "observe")
        page = MagicMock()
        decision = preflight_dialog_response(page, dialog_type="confirm", response="accept", site="test")
        assert decision is not None and decision.outcome is PolicyOutcome.DENIED

        accepts: dict[str, object] = {}
        for mode in ("disabled", "observe"):
            monkeypatch.setattr(settings, "BROWSER_ACTION_POLICY_MODE", mode)
            dialog = _make_dialog("confirm", "Are you sure?")
            await dialog_handler._handle_dialog(dialog, page=page)
            accepts[mode] = (dialog.accept.await_args_list, dialog.dismiss.await_args_list)
        assert accepts["disabled"] == accepts["observe"]

    @pytest.mark.asyncio
    async def test_registration_hands_the_handler_its_own_page(
        self, isolated_context: SkyvernContext, preflight_spy: list[dict]
    ) -> None:
        # "Dialog registration records the originating page": the listener is bound to the page it
        # was registered on, so the preflight always receives that page and never a guess.
        registered: dict[str, object] = {}

        page = MagicMock()
        page.on = lambda event, handler: registered.update({event: handler})
        browser_context = MagicMock()
        browser_context.pages = [page]
        browser_context.on = MagicMock()

        dialog_handler._registered_contexts.discard(browser_context)
        dialog_handler.set_dialog_handler(browser_context)
        listener = registered["dialog"]
        await listener(_make_dialog("confirm", "Sure?"))
        assert [call["page"] for call in preflight_spy] == [page]

    def test_a_page_is_never_double_registered(self) -> None:
        # A fresh partial per registration has no pyee identity dedupe; the page guard restores
        # it, or a double-registered page would answer one dialog twice.
        registrations: list[object] = []
        page = MagicMock()
        page.on = lambda event, handler: registrations.append(handler)
        for _ in range(2):
            browser_context = MagicMock()
            browser_context.pages = [page]
            browser_context.on = MagicMock()
            dialog_handler.set_dialog_handler(browser_context)
        assert len(registrations) == 1
