"""Commit-evidence for the LLM autocomplete option-selection path.

When the agent clicks the LLM-selected autocomplete option, evidence is captured only when the
target control's read-back changes and BOTH the pre-click and post-click values are
boundary-delimited fragments of the clicked option's label — i.e. the observed transition is
selection-specific, not an unrelated blur/masking/validation/restoration transform. The evidence
(the clicked option's label plus the control's post-click value) is serialized into action history
as an observation a later step or the completion verifier can lean on without reopening the
control. A no-op click, an identity display, an unrelated transform, an empty read-back, a failed
capture, or a secret value all leave a bare success with no evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.models import StepStatus
from skyvern.services.action_service import get_action_history
from skyvern.webeye.actions import handler
from skyvern.webeye.actions.actions import ClickAction, InputOrSelectContext, InputTextAction
from skyvern.webeye.actions.responses import ActionResult, ActionSuccess
from tests.unit.helpers import make_organization, make_step, make_task

_NOW = datetime.now(UTC)
_ORG = make_organization(_NOW)
_TASK = make_task(_NOW, _ORG, navigation_payload={"region": "California"})
_STEP = make_step(_NOW, _TASK, step_id="stp-1", status=StepStatus.created, order=0, output=None)

_OPTION_TEXT = "Backend Engineer"
_COMMITTED_VALUE = "Backend Engineer (Remote)"

# A composite option whose visible label decorates the identity the user typed
# ("CA - California" clicked, control closes onto "CA - California"). The typed
# probe left "California" in the control, so the click produces a real transition.
_COMPOSITE_LABEL = "CA - California"
_TYPED_PRE = "California"


def _control(tag: str = "input") -> MagicMock:
    element = MagicMock()
    element.get_tag_name.return_value = tag
    element.get_locator.return_value = MagicMock()
    return element


async def _run_producer(
    *,
    pre_values: list[str | None],
    option_label: str | None,
    static_element: dict | None = None,
    is_secret_value: bool = False,
    click: AsyncMock | None = None,
    skyvern_frame: MagicMock | None = None,
) -> ActionResult:
    """Drive the real producer with light IO stubs; pre_values feeds get_input_value in order."""
    click = click or AsyncMock()
    if skyvern_frame is None:
        skyvern_frame = MagicMock()
        skyvern_frame.safe_wait_for_animation_end = AsyncMock()
    identity = {"label": option_label} if option_label is not None else None
    with (
        patch.object(handler, "get_input_value", AsyncMock(side_effect=pre_values)),
        patch.object(handler, "_read_autocomplete_option_identity", AsyncMock(return_value=identity)),
    ):
        return await handler._click_autocomplete_option_with_commit_evidence(
            skyvern_element=_control(),
            option_locator=MagicMock(),
            option_static_element=static_element,
            skyvern_frame=skyvern_frame,
            click=click,
            is_secret_value=is_secret_value,
        )


async def _history_for(action_and_results: list) -> list[dict]:
    """Run the real get_action_history projection over a hand-built step output."""
    step = MagicMock()
    step.output = SimpleNamespace(actions_and_results=action_and_results)
    task = MagicMock(task_id="tsk_1", organization_id="o_1")
    with patch("skyvern.services.action_service.app") as app_mock:
        app_mock.DATABASE.tasks.get_task_steps = AsyncMock(return_value=[])
        return await get_action_history(task, current_step=step)


class TestCommitEvidenceModel:
    def test_fields_default_absent(self) -> None:
        r = ActionResult(success=True)
        assert r.committed_option is None
        assert r.committed_value is None

    def test_bare_success_has_no_evidence(self) -> None:
        r = ActionSuccess()
        assert r.committed_option is None
        assert r.committed_value is None

    def test_success_carries_evidence(self) -> None:
        r = ActionSuccess(committed_option=_OPTION_TEXT, committed_value=_COMMITTED_VALUE)
        assert r.success is True
        assert r.committed_option == _OPTION_TEXT
        assert r.committed_value == _COMMITTED_VALUE

    def test_evidence_in_str_representation(self) -> None:
        s = str(ActionSuccess(committed_option=_OPTION_TEXT, committed_value=_COMMITTED_VALUE))
        assert "committed_option=" in s
        assert "committed_value=" in s


class TestCommitEvidenceGate:
    """The pure transition gate: evidence only on a nonempty, normalized pre→post difference."""

    def test_emits_on_genuine_transition(self) -> None:
        assert handler._autocomplete_commit_evidence(_TYPED_PRE, _COMPOSITE_LABEL, _COMPOSITE_LABEL) == (
            _COMPOSITE_LABEL,
            _COMPOSITE_LABEL,
        )

    def test_emits_when_pre_and_post_are_boundary_fragments_of_composite_label(self) -> None:
        # The motivating shape: the account digits the user typed and the customer-name display the
        # control closes onto are BOTH boundary-delimited fragments of one composite option label.
        assert handler._autocomplete_commit_evidence("12345", "Acme Corp", "12345 - Acme Corp") == (
            "12345 - Acme Corp",
            "Acme Corp",
        )

    def test_emits_state_code_transition(self) -> None:
        # user enters "California"; control closes onto the state code "CA"; both fragments of label.
        assert handler._autocomplete_commit_evidence("California", "CA", "CA - California") == ("CA - California", "CA")

    @pytest.mark.parametrize(
        "pre, post, label",
        [
            # An unrelated blur/formatting transform (phone) yields a post that is not a fragment of the label.
            ("California", "(415) 555-1234", "CA - California"),
            # Restoration to a different stale/invalid value after a failed selection.
            ("California", "Texas", "CA - California"),
            # "CA" merely appears inside "California" — not a boundary-delimited fragment.
            ("California", "CA", "California"),
            # Numeric prefix: "1234" sits inside the digit run "12345", not a boundary fragment.
            ("1234", "Acme Corp", "12345 - Acme Corp"),
            # Punctuation-only post carries no alphanumeric, so it can never be selection-specific.
            ("California", "—", "CA — California"),
            # Blur-time masking of the typed value is not a fragment of the option label.
            ("1234567890", "••••7890", "Account 1234567890"),
            # CJK interior: adjacent CJK chars are word chars, so an interior fragment fails the boundary
            # lookarounds and evidence is withheld (documented fail-closed recall loss).
            ("東京", "東京都", "東京都"),
        ],
    )
    def test_non_selection_transitions_fail_closed(self, pre: str, post: str, label: str) -> None:
        # Each is a nonempty, differing pre->post that the old gate accepted; requiring both to be
        # boundary-delimited fragments of the clicked option label rejects them.
        assert handler._autocomplete_commit_evidence(pre, post, label) is None

    def test_no_evidence_when_post_equals_pre(self) -> None:
        assert handler._autocomplete_commit_evidence(_TYPED_PRE, _TYPED_PRE, _COMPOSITE_LABEL) is None

    def test_no_evidence_when_post_normalizes_to_pre(self) -> None:
        assert handler._autocomplete_commit_evidence("California", "  california ", _COMPOSITE_LABEL) is None

    def test_no_evidence_when_post_empty(self) -> None:
        assert handler._autocomplete_commit_evidence(_TYPED_PRE, "", _COMPOSITE_LABEL) is None

    def test_no_evidence_when_pre_empty(self) -> None:
        assert handler._autocomplete_commit_evidence("", _COMPOSITE_LABEL, _COMPOSITE_LABEL) is None

    def test_no_evidence_when_label_empty(self) -> None:
        assert handler._autocomplete_commit_evidence(_TYPED_PRE, _COMPOSITE_LABEL, "") is None

    @pytest.mark.parametrize(
        "pre, post, label",
        [
            (" ", _COMPOSITE_LABEL, _COMPOSITE_LABEL),  # whitespace-only pre normalizes to empty
            (_TYPED_PRE, "   ", _COMPOSITE_LABEL),  # whitespace-only post normalizes to empty
            (_TYPED_PRE, _COMPOSITE_LABEL, " \t\n "),  # whitespace-only label normalizes to empty
            ("\xa0", _COMPOSITE_LABEL, _COMPOSITE_LABEL),  # non-breaking space only
        ],
    )
    def test_whitespace_only_fields_fail_closed(self, pre: str, post: str, label: str) -> None:
        # A field that survives the truthiness check but normalizes to empty must not mint evidence.
        assert handler._autocomplete_commit_evidence(pre, post, label) is None

    def test_fields_truncated_after_relation_runs_on_full_strings(self) -> None:
        # pre and post are valid boundary fragments of the full label; the fragment relation is
        # evaluated on the full normalized strings, and only the emitted fields are then capped.
        limit = handler.SELECT_SHADOW_MATCH_FIELD_MAX_CHARS
        pre = _TYPED_PRE
        post = "california " + "y" * (limit + 40)
        label = post + " suffix"
        evidence = handler._autocomplete_commit_evidence(pre, post, label)
        assert evidence is not None
        committed_option, committed_value = evidence
        assert committed_option == label[:limit] + "…"
        assert committed_value == post[:limit] + "…"


class TestCommitEvidenceProducer:
    """The LLM-path producer: click, then evidence only on a measured control transition."""

    @pytest.mark.asyncio
    async def test_transition_emits_live_label_and_post_value(self) -> None:
        result = await _run_producer(
            pre_values=[_TYPED_PRE, _COMPOSITE_LABEL, _COMPOSITE_LABEL],
            option_label=_COMPOSITE_LABEL,
        )
        assert isinstance(result, ActionSuccess)
        assert result.committed_option == _COMPOSITE_LABEL
        assert result.committed_value == _COMPOSITE_LABEL

    @pytest.mark.asyncio
    async def test_noop_click_leaves_pre_unchanged_no_evidence(self) -> None:
        # Codex r3881398576 regression: the click does not commit, control still holds the probe.
        click = AsyncMock()
        result = await _run_producer(
            pre_values=[_TYPED_PRE, _TYPED_PRE, _TYPED_PRE],
            option_label=_COMPOSITE_LABEL,
            click=click,
        )
        assert isinstance(result, ActionSuccess)
        assert result.committed_option is None
        assert result.committed_value is None
        click.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_masked_pre_differs_from_typed_text_noop_no_evidence(self) -> None:
        # The measured pre-click value is a masked form, not the typed text; a no-op click that
        # leaves that masked form in place must not emit (proves measured pre, not typed text).
        result = await _run_producer(
            pre_values=["(•••) masked", "(•••) masked", "(•••) masked"],
            option_label=_COMPOSITE_LABEL,
        )
        assert result.committed_option is None
        assert result.committed_value is None

    @pytest.mark.asyncio
    async def test_readback_changes_to_value_not_in_label_no_evidence(self) -> None:
        # The click flips the control to an unrelated reformatted value (blur masking / validation /
        # restoration) that is not a fragment of the clicked option label; a differing post is no
        # longer accepted as selection-specific, so the click stays a bare success.
        result = await _run_producer(
            pre_values=[_TYPED_PRE, "(415) 555-1234"],
            option_label=_COMPOSITE_LABEL,
        )
        assert isinstance(result, ActionSuccess)
        assert result.committed_option is None
        assert result.committed_value is None

    @pytest.mark.asyncio
    async def test_empty_post_no_evidence(self) -> None:
        result = await _run_producer(
            pre_values=[_TYPED_PRE, ""],
            option_label=_COMPOSITE_LABEL,
        )
        assert result.committed_option is None
        assert result.committed_value is None

    @pytest.mark.asyncio
    async def test_optimistic_label_that_reverts_after_settle_no_evidence(self) -> None:
        # Codex r3892628198 regression: the control optimistically paints the selected label (a real
        # transition off the typed probe, so the candidate gate passes) and then reverts to the probe
        # after async validation/rerender. The first post read looked committed; the settled reread
        # proves it was transient, so no stale evidence may be recorded.
        skyvern_frame = MagicMock()
        skyvern_frame.safe_wait_for_animation_end = AsyncMock()
        result = await _run_producer(
            pre_values=[_TYPED_PRE, _COMPOSITE_LABEL, _TYPED_PRE],
            option_label=_COMPOSITE_LABEL,
            skyvern_frame=skyvern_frame,
        )
        assert isinstance(result, ActionSuccess)
        assert result.committed_option is None
        assert result.committed_value is None
        skyvern_frame.safe_wait_for_animation_end.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_timer_style_revert_within_settle_floor_withholds_evidence(self) -> None:
        # Codex r3892846834 (P1): a non-animation revert — a ~300ms timer or async validation, not a
        # CSS/Web Animation — leaves document.getAnimations() empty, so safe_wait_for_animation_end has
        # nothing to await and only its before_wait_sec floor holds the confirm read back. A virtual
        # clock advanced solely by that floor (the settle's own before_wait_sec) proves the outcome at
        # an entry point above the wait: without a bounded floor the settled reread fires while the
        # optimistic label is still painted and commits a value that vanishes at 300ms; a floor past
        # 300ms observes the reverted probe and withholds evidence.
        revert_at_ms = 300.0
        clock = SimpleNamespace(ms=0.0)
        reads = iter([_TYPED_PRE, _COMPOSITE_LABEL])

        async def settle(*, before_wait_sec: float = 0, caller: str = "") -> None:
            clock.ms += before_wait_sec * 1000

        def read_control(*_args: object, **_kwargs: object) -> str:
            try:
                return next(reads)
            except StopIteration:
                return _TYPED_PRE if clock.ms >= revert_at_ms else _COMPOSITE_LABEL

        skyvern_frame = MagicMock()
        skyvern_frame.safe_wait_for_animation_end = AsyncMock(side_effect=settle)
        with (
            patch.object(handler, "get_input_value", AsyncMock(side_effect=read_control)),
            patch.object(
                handler, "_read_autocomplete_option_identity", AsyncMock(return_value={"label": _COMPOSITE_LABEL})
            ),
        ):
            result = await handler._click_autocomplete_option_with_commit_evidence(
                skyvern_element=_control(),
                option_locator=MagicMock(),
                option_static_element=None,
                skyvern_frame=skyvern_frame,
                click=AsyncMock(),
                is_secret_value=False,
            )
        assert isinstance(result, ActionSuccess)
        assert result.committed_option is None
        assert result.committed_value is None

    @pytest.mark.asyncio
    async def test_confirm_read_failure_after_candidate_no_evidence(self) -> None:
        # The candidate gate passes on the first post read, but the settled confirm read fails
        # (returns None). A value that cannot be reconfirmed after settling must fail closed.
        result = await _run_producer(
            pre_values=[_TYPED_PRE, _COMPOSITE_LABEL, RuntimeError("read boom")],
            option_label=_COMPOSITE_LABEL,
        )
        assert isinstance(result, ActionSuccess)
        assert result.committed_option is None
        assert result.committed_value is None

    @pytest.mark.asyncio
    async def test_confirm_drift_to_other_value_no_evidence(self) -> None:
        # The candidate passes, but the settled reread drifts to a different value than the first
        # post read. Emission requires the transitioned value to equal the first post, not merely to
        # differ from pre, so a drift withholds evidence.
        result = await _run_producer(
            pre_values=[_TYPED_PRE, _COMPOSITE_LABEL, "Texas"],
            option_label=_COMPOSITE_LABEL,
        )
        assert isinstance(result, ActionSuccess)
        assert result.committed_option is None
        assert result.committed_value is None

    @pytest.mark.asyncio
    async def test_stable_transition_survives_settle_emits(self) -> None:
        # The transitioned value is reread identically after the single bounded settle, so the
        # selection is stable and the evidence is recorded.
        skyvern_frame = MagicMock()
        skyvern_frame.safe_wait_for_animation_end = AsyncMock()
        result = await _run_producer(
            pre_values=[_TYPED_PRE, _COMPOSITE_LABEL, _COMPOSITE_LABEL],
            option_label=_COMPOSITE_LABEL,
            skyvern_frame=skyvern_frame,
        )
        assert result.committed_option == _COMPOSITE_LABEL
        assert result.committed_value == _COMPOSITE_LABEL
        skyvern_frame.safe_wait_for_animation_end.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_label_read_failure_click_still_succeeds_no_evidence(self) -> None:
        click = AsyncMock()
        result = await _run_producer(
            pre_values=[_TYPED_PRE, _COMPOSITE_LABEL],
            option_label=None,  # identity read returns None
            static_element=None,  # no scraped fallback text either
            click=click,
        )
        assert isinstance(result, ActionSuccess)
        assert result.committed_option is None
        assert result.committed_value is None
        click.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_static_element_text_is_label_fallback(self) -> None:
        result = await _run_producer(
            pre_values=[_TYPED_PRE, _COMPOSITE_LABEL, _COMPOSITE_LABEL],
            option_label=None,
            static_element={"text": _COMPOSITE_LABEL},
        )
        assert result.committed_option == _COMPOSITE_LABEL
        assert result.committed_value == _COMPOSITE_LABEL

    @pytest.mark.asyncio
    async def test_secret_value_never_emits_and_skips_reads(self) -> None:
        click = AsyncMock()
        skyvern_frame = MagicMock()
        get_input_value = AsyncMock(return_value=_COMPOSITE_LABEL)
        identity = AsyncMock(return_value={"label": _COMPOSITE_LABEL})
        with (
            patch.object(handler, "get_input_value", get_input_value),
            patch.object(handler, "_read_autocomplete_option_identity", identity),
        ):
            result = await handler._click_autocomplete_option_with_commit_evidence(
                skyvern_element=_control(),
                option_locator=MagicMock(),
                option_static_element={"text": _COMPOSITE_LABEL},
                skyvern_frame=skyvern_frame,
                click=click,
                is_secret_value=True,
            )
        assert isinstance(result, ActionSuccess)
        assert result.committed_option is None
        assert result.committed_value is None
        click.assert_awaited_once()
        get_input_value.assert_not_called()
        identity.assert_not_called()

    @pytest.mark.asyncio
    async def test_overlong_label_and_post_capped(self) -> None:
        limit = handler.SELECT_SHADOW_MATCH_FIELD_MAX_CHARS
        long_post = "california " + "p" * (limit + 50)
        long_label = long_post + " suffix"
        result = await _run_producer(
            pre_values=[_TYPED_PRE, long_post, long_post],
            option_label=long_label,
        )
        assert result.committed_option == long_label[:limit] + "…"
        assert result.committed_value == long_post[:limit] + "…"

    @pytest.mark.asyncio
    async def test_capture_exception_after_click_stays_success(self) -> None:
        click = AsyncMock()
        skyvern_frame = MagicMock()
        skyvern_frame.safe_wait_for_animation_end = AsyncMock()
        with (
            patch.object(handler, "get_input_value", AsyncMock(side_effect=[_TYPED_PRE, _COMPOSITE_LABEL])),
            patch.object(
                handler, "_read_autocomplete_option_identity", AsyncMock(return_value={"label": _COMPOSITE_LABEL})
            ),
            patch.object(handler, "_autocomplete_commit_evidence", side_effect=RuntimeError("boom")),
        ):
            result = await handler._click_autocomplete_option_with_commit_evidence(
                skyvern_element=_control(),
                option_locator=MagicMock(),
                option_static_element=None,
                skyvern_frame=skyvern_frame,
                click=click,
                is_secret_value=False,
            )
        assert isinstance(result, ActionSuccess)
        assert result.success is True
        assert result.committed_option is None
        click.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_failed_click_propagates_not_swallowed(self) -> None:
        click = AsyncMock(side_effect=RuntimeError("click failed"))
        skyvern_frame = MagicMock()
        identity = AsyncMock(return_value={"label": _COMPOSITE_LABEL})
        with (
            patch.object(handler, "get_input_value", AsyncMock(return_value=_TYPED_PRE)),
            patch.object(handler, "_read_autocomplete_option_identity", identity),
            pytest.raises(RuntimeError),
        ):
            await handler._click_autocomplete_option_with_commit_evidence(
                skyvern_element=_control(),
                option_locator=MagicMock(),
                option_static_element=None,
                skyvern_frame=skyvern_frame,
                click=click,
                is_secret_value=False,
            )


class TestWrapperPreservesEvidence:
    """input_or_auto_complete_input must forward the producer's enriched result, not a bare success."""

    @pytest.mark.asyncio
    async def test_wrapper_forwards_enriched_result_first_attempt(self) -> None:
        enriched = ActionSuccess(committed_option=_COMPOSITE_LABEL, committed_value=_COMPOSITE_LABEL)
        producer_result = handler.AutoCompletionResult(action_result=enriched)
        context = SimpleNamespace(is_location_input=False, is_search_bar=False)
        element = MagicMock()
        element.get_id.return_value = "region-combobox"
        with patch.object(handler, "choose_auto_completion_dropdown", AsyncMock(return_value=producer_result)):
            returned = await handler.input_or_auto_complete_input(
                input_or_select_context=context,
                scraped_page=MagicMock(),
                page=MagicMock(),
                dom=MagicMock(),
                text=_TYPED_PRE,
                skyvern_element=element,
                step=MagicMock(),
                task=MagicMock(),
                is_secret_value=False,
            )
        assert returned is not None
        assert returned.committed_option == _COMPOSITE_LABEL
        assert returned.committed_value == _COMPOSITE_LABEL


class TestChooseDropdownSeamWiring:
    """The LLM-selected-option branch of the real choose_auto_completion_dropdown routes the
    clicked option through the commit-evidence producer. A rendered-autocomplete option (keyed
    by id+text, not a native <select>) is chosen by the LLM; a measured control transition emits
    evidence, and a secret value suppresses it while the click still lands. Locks the seam at the
    choose→producer hop (control-vs-option element, is_secret_value forwarding)."""

    @staticmethod
    def _wire(*, control_reads: list[str], option_label: str) -> dict:
        option = {"id": "opt-ca", "tag": "div", "text": option_label}

        option_locator = MagicMock()
        option_locator.count = AsyncMock(return_value=1)
        option_locator.element_handle = AsyncMock(return_value=MagicMock())

        frame = MagicMock()
        frame.locator.return_value = option_locator

        control_locator = MagicMock()
        control_locator.input_value = AsyncMock(side_effect=list(control_reads))

        control = MagicMock()
        control.get_id.return_value = "region-combobox"
        control.get_frame.return_value = frame
        control.get_frame_id.return_value = "frame-1"
        control.is_interactable.return_value = True
        control.get_tag_name.return_value = "input"
        control.get_locator.return_value = control_locator
        control.press_fill = AsyncMock()
        control.input_clear = AsyncMock()
        control.is_visible = AsyncMock(return_value=True)
        control.get_element_handler = AsyncMock(return_value=MagicMock())

        skyvern_frame = MagicMock(safe_wait_for_animation_end=AsyncMock())
        skyvern_frame.read_autocomplete_option_identity = AsyncMock(return_value={"index": 0, "label": option_label})

        inc = MagicMock()
        inc.start_listen_dom_increment = AsyncMock()
        inc.stop_listen_dom_increment = AsyncMock()
        inc.get_incremental_element_tree = AsyncMock(return_value=[dict(option)])
        inc.id_to_element_dict = {"opt-ca": dict(option)}
        inc.build_html_tree.return_value = "<div>opt</div>"

        return {
            "control": control,
            "control_locator": control_locator,
            "skyvern_frame": skyvern_frame,
            "inc": inc,
        }

    async def _run_choose(self, wired: dict, *, is_secret_value: bool):
        clicked_option = MagicMock(scroll_into_view=AsyncMock(), click=AsyncMock())
        llm_response = {"relevance_float": 0.95, "id": "opt-ca", "direct_searching": False, "reasoning": "match"}
        with (
            patch(
                "skyvern.webeye.actions.handler.SkyvernFrame.create_instance",
                new=AsyncMock(return_value=wired["skyvern_frame"]),
            ),
            patch("skyvern.webeye.actions.handler.IncrementalScrapePage", return_value=wired["inc"]),
            patch("skyvern.webeye.actions.handler.SkyvernElement", return_value=clicked_option),
            patch("skyvern.webeye.actions.handler.app") as mock_app,
            patch("skyvern.webeye.actions.handler.prompt_engine") as mock_prompt,
            patch("skyvern.webeye.actions.handler.skyvern_context") as mock_ctx,
        ):
            mock_app.AUTO_COMPLETION_LLM_API_HANDLER = AsyncMock(return_value=llm_response)
            mock_app.AGENT_FUNCTION = MagicMock()
            mock_prompt.load_prompt.return_value = "prompt"
            mock_ctx.ensure_context.return_value = MagicMock(tz_info=UTC)
            result = await handler.choose_auto_completion_dropdown(
                context=InputOrSelectContext(field="Region", is_search_bar=False),
                page=MagicMock(),
                scraped_page=MagicMock(),
                dom=MagicMock(),
                text=_TYPED_PRE,
                skyvern_element=wired["control"],
                step=_STEP,
                task=_TASK,
                is_secret_value=is_secret_value,
            )
        return result.action_result, clicked_option

    @pytest.mark.asyncio
    async def test_llm_selected_option_transition_emits_evidence(self) -> None:
        wired = self._wire(
            control_reads=[_TYPED_PRE, _COMPOSITE_LABEL, _COMPOSITE_LABEL], option_label=_COMPOSITE_LABEL
        )
        action_result, clicked_option = await self._run_choose(wired, is_secret_value=False)

        assert isinstance(action_result, ActionSuccess)
        assert action_result.committed_option == _COMPOSITE_LABEL
        assert action_result.committed_value == _COMPOSITE_LABEL
        clicked_option.click.assert_awaited_once()
        # Evidence must come from the control's read-back (pre, post, settled confirm), not the option node.
        assert wired["control_locator"].input_value.await_count == 3

    @pytest.mark.asyncio
    async def test_secret_value_suppresses_evidence_but_click_lands(self) -> None:
        wired = self._wire(control_reads=[_TYPED_PRE, _COMPOSITE_LABEL], option_label=_COMPOSITE_LABEL)
        action_result, clicked_option = await self._run_choose(wired, is_secret_value=True)

        assert isinstance(action_result, ActionSuccess)
        assert action_result.committed_option is None
        assert action_result.committed_value is None
        clicked_option.click.assert_awaited_once()
        wired["control_locator"].input_value.assert_not_called()
        wired["skyvern_frame"].read_autocomplete_option_identity.assert_not_called()


class TestControlReadTimeoutBound:
    """The commit-evidence control read is bounded by a small explicit timeout so the added settle
    reread cannot inherit Playwright's 30s default wait; the global get_input_value stays unbounded."""

    @pytest.mark.asyncio
    async def test_control_read_bounded_by_explicit_timeout(self) -> None:
        locator = MagicMock()
        locator.input_value = AsyncMock(return_value=_TYPED_PRE)
        element = MagicMock()
        element.get_tag_name.return_value = "input"
        element.get_locator.return_value = locator

        result = await handler._read_autocomplete_control_value(element)

        assert result == _TYPED_PRE
        locator.input_value.assert_awaited_once_with(timeout=handler.settings.BROWSER_ACTION_TIMEOUT_MS)

    @pytest.mark.asyncio
    async def test_get_input_value_unbounded_by_default(self) -> None:
        # Other callers keep stock Playwright behavior: no timeout is threaded unless asked for.
        locator = MagicMock()
        locator.input_value = AsyncMock(return_value="x")

        await handler.get_input_value("input", locator)

        locator.input_value.assert_awaited_once_with()


class TestCommitEvidenceSerialization:
    @pytest.mark.asyncio
    async def test_positive_readback_serialized_into_history(self) -> None:
        action = InputTextAction(element_id="job-title", text="Backend")
        result = ActionSuccess(committed_option=_OPTION_TEXT, committed_value=_COMMITTED_VALUE)

        history = await _history_for([(action, [result])])

        assert len(history) == 1
        assert history[0]["result"]["committed_option"] == _OPTION_TEXT
        assert history[0]["result"]["committed_value"] == _COMMITTED_VALUE

    @pytest.mark.asyncio
    async def test_unverified_success_omits_evidence(self) -> None:
        action = InputTextAction(element_id="job-title", text="Backend")
        history = await _history_for([(action, [ActionSuccess()])])

        assert "committed_option" not in history[0]["result"]
        assert "committed_value" not in history[0]["result"]

    @pytest.mark.asyncio
    async def test_adjacent_action_history_unchanged(self) -> None:
        target = InputTextAction(element_id="job-title", text="Backend")
        target_result = ActionSuccess(committed_option=_OPTION_TEXT, committed_value=_COMMITTED_VALUE)
        neighbor = ClickAction(element_id="submit")
        neighbor_result = ActionSuccess()

        history = await _history_for([(neighbor, [neighbor_result]), (target, [target_result])])

        assert "committed_option" not in history[0]["result"]
        assert "committed_value" not in history[0]["result"]
        assert history[1]["result"]["committed_option"] == _OPTION_TEXT
        assert history[1]["result"]["committed_value"] == _COMMITTED_VALUE
