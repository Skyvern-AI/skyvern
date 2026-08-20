"""Per-run resolution of the PRESERVE_TRANSIENT_UI_CAPTURE experiment.

Off/unenrolled is the safe default: the flag only assigns treatment when a real provider resolves
True. Undefined, no-provider, resolver error, and missing run identity all stay off/unenrolled
(the predicate is not evaluated and scrolling is unchanged). An explicit provider False is the
distinct enrolled control arm (shadow-detect only, still scrolls).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.experimentation import transient_ui_capture as tuc
from skyvern.forge.sdk.experimentation.providers import NoOpExperimentationProvider


def _ctx(**kwargs: object) -> SkyvernContext:
    return SkyvernContext(**kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_provider_true_caches_treatment(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(workflow_run_id="wr_1", organization_id="o_1", workflow_permanent_id="wpid_1")
    provider = MagicMock()
    provider.resolve_feature_flag_cached = AsyncMock(return_value=True)
    monkeypatch.setattr(tuc.app, "EXPERIMENTATION_PROVIDER", provider)
    await tuc.resolve_transient_ui_capture_arm(ctx)
    assert ctx.preserve_transient_ui_capture is True
    assert tuc.transient_ui_capture_arm(ctx) == "treatment"
    provider.resolve_feature_flag_cached.assert_awaited_once()
    # run-level distinct_id, org/wpid as properties (per backend.md convention)
    call = provider.resolve_feature_flag_cached.await_args
    assert call.args[0] == tuc.PRESERVE_TRANSIENT_UI_CAPTURE_FLAG
    assert call.args[1] == "wr_1"
    assert call.kwargs["properties"]["organization_id"] == "o_1"


@pytest.mark.asyncio
async def test_provider_false_caches_control(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(workflow_run_id="wr_1")
    provider = MagicMock()
    provider.resolve_feature_flag_cached = AsyncMock(return_value=False)
    monkeypatch.setattr(tuc.app, "EXPERIMENTATION_PROVIDER", provider)
    await tuc.resolve_transient_ui_capture_arm(ctx)
    assert ctx.preserve_transient_ui_capture is False
    assert tuc.transient_ui_capture_arm(ctx) == "control"


@pytest.mark.asyncio
async def test_provider_undefined_none_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(workflow_run_id="wr_1")
    provider = MagicMock()
    provider.resolve_feature_flag_cached = AsyncMock(return_value=None)
    monkeypatch.setattr(tuc.app, "EXPERIMENTATION_PROVIDER", provider)
    await tuc.resolve_transient_ui_capture_arm(ctx)
    assert ctx.preserve_transient_ui_capture is None
    assert tuc.transient_ui_capture_arm(ctx) == "off"


@pytest.mark.asyncio
async def test_noop_provider_is_off_without_querying_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(workflow_run_id="wr_1")
    provider = NoOpExperimentationProvider()
    spy = AsyncMock(return_value=True)
    monkeypatch.setattr(provider, "resolve_feature_flag_cached", spy)
    monkeypatch.setattr(tuc.app, "EXPERIMENTATION_PROVIDER", provider)
    await tuc.resolve_transient_ui_capture_arm(ctx)
    assert tuc.transient_ui_capture_arm(ctx) == "off"
    spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_error_is_off_and_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(workflow_run_id="wr_1")
    provider = MagicMock()
    provider.resolve_feature_flag_cached = AsyncMock(side_effect=RuntimeError("posthog down"))
    monkeypatch.setattr(tuc.app, "EXPERIMENTATION_PROVIDER", provider)
    await tuc.resolve_transient_ui_capture_arm(ctx)
    assert tuc.transient_ui_capture_arm(ctx) == "off"


@pytest.mark.asyncio
async def test_missing_run_identity_is_off_without_querying_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()
    provider = MagicMock()
    provider.resolve_feature_flag_cached = AsyncMock(return_value=True)
    monkeypatch.setattr(tuc.app, "EXPERIMENTATION_PROVIDER", provider)
    await tuc.resolve_transient_ui_capture_arm(ctx)
    assert tuc.transient_ui_capture_arm(ctx) == "off"
    provider.resolve_feature_flag_cached.assert_not_awaited()


def test_arm_helper_missing_context_is_off() -> None:
    assert tuc.transient_ui_capture_arm(None) == "off"


def test_arm_helper_uninitialized_field_is_off() -> None:
    assert tuc.transient_ui_capture_arm(_ctx(workflow_run_id="wr_1")) == "off"


@pytest.mark.asyncio
async def test_treatment_arm_is_sticky_across_provider_change(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(workflow_run_id="wr_1")
    provider = MagicMock()
    provider.resolve_feature_flag_cached = AsyncMock(return_value=True)
    monkeypatch.setattr(tuc.app, "EXPERIMENTATION_PROVIDER", provider)
    await tuc.resolve_transient_ui_capture_arm(ctx)
    assert tuc.transient_ui_capture_arm(ctx) == "treatment"
    # TTL expiry / mid-run ramp flips the provider result — the run's arm must NOT change.
    provider.resolve_feature_flag_cached.return_value = False
    await tuc.resolve_transient_ui_capture_arm(ctx)
    assert tuc.transient_ui_capture_arm(ctx) == "treatment"
    provider.resolve_feature_flag_cached.assert_awaited_once()


@pytest.mark.asyncio
async def test_control_arm_is_sticky_across_provider_change(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(workflow_run_id="wr_1")
    provider = MagicMock()
    provider.resolve_feature_flag_cached = AsyncMock(return_value=False)
    monkeypatch.setattr(tuc.app, "EXPERIMENTATION_PROVIDER", provider)
    await tuc.resolve_transient_ui_capture_arm(ctx)
    assert tuc.transient_ui_capture_arm(ctx) == "control"
    provider.resolve_feature_flag_cached.return_value = True
    await tuc.resolve_transient_ui_capture_arm(ctx)
    assert tuc.transient_ui_capture_arm(ctx) == "control"
    provider.resolve_feature_flag_cached.assert_awaited_once()


@pytest.mark.asyncio
async def test_off_undefined_arm_is_sticky_across_provider_change(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(workflow_run_id="wr_1")
    provider = MagicMock()
    provider.resolve_feature_flag_cached = AsyncMock(return_value=None)
    monkeypatch.setattr(tuc.app, "EXPERIMENTATION_PROVIDER", provider)
    await tuc.resolve_transient_ui_capture_arm(ctx)
    assert tuc.transient_ui_capture_arm(ctx) == "off"
    provider.resolve_feature_flag_cached.return_value = True
    await tuc.resolve_transient_ui_capture_arm(ctx)
    assert tuc.transient_ui_capture_arm(ctx) == "off"
    provider.resolve_feature_flag_cached.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolver_error_arm_is_sticky_off(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(workflow_run_id="wr_1")
    provider = MagicMock()
    provider.resolve_feature_flag_cached = AsyncMock(side_effect=RuntimeError("posthog down"))
    monkeypatch.setattr(tuc.app, "EXPERIMENTATION_PROVIDER", provider)
    await tuc.resolve_transient_ui_capture_arm(ctx)
    assert tuc.transient_ui_capture_arm(ctx) == "off"
    # Provider recovers and would now return treatment — the pinned off must not flip.
    recovered = AsyncMock(return_value=True)
    monkeypatch.setattr(provider, "resolve_feature_flag_cached", recovered)
    await tuc.resolve_transient_ui_capture_arm(ctx)
    assert tuc.transient_ui_capture_arm(ctx) == "off"
    recovered.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_identity_arm_is_sticky_off(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()  # no run identity
    provider = MagicMock()
    provider.resolve_feature_flag_cached = AsyncMock(return_value=True)
    monkeypatch.setattr(tuc.app, "EXPERIMENTATION_PROVIDER", provider)
    await tuc.resolve_transient_ui_capture_arm(ctx)
    assert tuc.transient_ui_capture_arm(ctx) == "off"
    await tuc.resolve_transient_ui_capture_arm(ctx)
    assert tuc.transient_ui_capture_arm(ctx) == "off"
    provider.resolve_feature_flag_cached.assert_not_awaited()


@pytest.mark.asyncio
async def test_noop_provider_is_sticky_off_without_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(workflow_run_id="wr_1")
    provider = NoOpExperimentationProvider()
    spy = AsyncMock(return_value=True)
    monkeypatch.setattr(provider, "resolve_feature_flag_cached", spy)
    monkeypatch.setattr(tuc.app, "EXPERIMENTATION_PROVIDER", provider)
    await tuc.resolve_transient_ui_capture_arm(ctx)
    await tuc.resolve_transient_ui_capture_arm(ctx)
    assert tuc.transient_ui_capture_arm(ctx) == "off"
    spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_distinct_id_override_used_over_context(monkeypatch: pytest.MonkeyPatch) -> None:
    # An execution-boundary caller (e.g. an inline child workflow) whose context lacks full run
    # identity may pass distinct_id / organization_id / workflow_permanent_id explicitly.
    ctx = _ctx(run_id="parent_run", organization_id="o_ctx")
    provider = MagicMock()
    provider.resolve_feature_flag_cached = AsyncMock(return_value=True)
    monkeypatch.setattr(tuc.app, "EXPERIMENTATION_PROVIDER", provider)
    await tuc.resolve_transient_ui_capture_arm(
        ctx, distinct_id="wr_child", organization_id="o_child", workflow_permanent_id="wpid_child"
    )
    assert tuc.transient_ui_capture_arm(ctx) == "treatment"
    call = provider.resolve_feature_flag_cached.await_args
    assert call.args[1] == "wr_child"  # explicit distinct_id, NOT the context's parent run_id
    assert call.kwargs["properties"]["organization_id"] == "o_child"
    assert call.kwargs["properties"]["workflow_permanent_id"] == "wpid_child"


@pytest.mark.asyncio
async def test_override_falls_back_to_context_when_not_given(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(workflow_run_id="wr_ctx", organization_id="o_ctx")
    provider = MagicMock()
    provider.resolve_feature_flag_cached = AsyncMock(return_value=False)
    monkeypatch.setattr(tuc.app, "EXPERIMENTATION_PROVIDER", provider)
    await tuc.resolve_transient_ui_capture_arm(ctx)
    assert tuc.transient_ui_capture_arm(ctx) == "control"
    assert provider.resolve_feature_flag_cached.await_args.args[1] == "wr_ctx"


@pytest.mark.asyncio
async def test_concurrent_first_resolve_queries_provider_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two coroutines sharing one SkyvernContext both hit the first-resolve path. A single-flight lock
    # must keep the provider queried exactly once (the ADR's "provider ≤ once/run" invariant).
    ctx = _ctx(workflow_run_id="wr_1")
    calls = {"n": 0}

    async def _slow_resolve(*_a: object, **_k: object) -> bool:
        calls["n"] += 1
        await asyncio.sleep(0)  # yield so a concurrent first-resolver can interleave before we pin
        return True

    provider = MagicMock()
    provider.resolve_feature_flag_cached = AsyncMock(side_effect=_slow_resolve)
    monkeypatch.setattr(tuc.app, "EXPERIMENTATION_PROVIDER", provider)

    await asyncio.gather(
        tuc.resolve_transient_ui_capture_arm(ctx),
        tuc.resolve_transient_ui_capture_arm(ctx),
    )

    assert calls["n"] == 1, "two concurrent first-resolvers on one context must query the provider once"
    assert tuc.transient_ui_capture_arm(ctx) == "treatment"


# --- Shared capture helpers used by BOTH the agent-step scrape and the post-action screenshot ---


def test_emit_telemetry_omits_arbitrary_page_controlled_tokens() -> None:
    span = MagicMock()
    tuc.emit_transient_ui_popup_telemetry(
        span, {"role": "<script>evil", "hasPopup": "arbitrary-attacker-text", "controlsResolved": 0}
    )
    keys = {call.args[0] for call in span.set_attribute.call_args_list}
    assert "transient_ui_role" not in keys, "non-allowlisted role must be omitted"
    assert "transient_ui_haspopup" not in keys, "non-allowlisted haspopup must be omitted"


def test_emit_telemetry_emits_allowlisted_tokens_and_controls_boolean() -> None:
    span = MagicMock()
    tuc.emit_transient_ui_popup_telemetry(span, {"role": "combobox", "hasPopup": "menu", "controlsResolved": 2})
    attrs = {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}
    assert attrs["transient_ui_role"] == "combobox"
    assert attrs["transient_ui_haspopup"] == "menu"
    assert attrs["transient_ui_controls_resolved"] is True


def test_emit_telemetry_controls_boolean_false_for_no_target_fallback() -> None:
    span = MagicMock()
    tuc.emit_transient_ui_popup_telemetry(span, {"role": "combobox", "hasPopup": None, "controlsResolved": 0})
    attrs = {call.args[0]: call.args[1] for call in span.set_attribute.call_args_list}
    assert attrs["transient_ui_controls_resolved"] is False


def test_decide_suppression_treatment_caps_at_two_then_falls_back() -> None:
    ctx = _ctx(workflow_run_id="wr_cap", preserve_transient_ui_capture=True)
    d1 = tuc.decide_transient_ui_suppression(ctx, tuc.ARM_TREATMENT, detected=True)
    d2 = tuc.decide_transient_ui_suppression(ctx, tuc.ARM_TREATMENT, detected=True)
    d3 = tuc.decide_transient_ui_suppression(ctx, tuc.ARM_TREATMENT, detected=True)
    assert (d1.suppress, d2.suppress, d3.suppress) == (True, True, False)
    assert d3.capped is True
    assert ctx.transient_ui_consecutive_suppressions == 2


def test_decide_suppression_resets_when_not_detected() -> None:
    ctx = _ctx(workflow_run_id="wr_reset", preserve_transient_ui_capture=True)
    tuc.decide_transient_ui_suppression(ctx, tuc.ARM_TREATMENT, detected=True)
    tuc.decide_transient_ui_suppression(ctx, tuc.ARM_TREATMENT, detected=True)
    assert ctx.transient_ui_consecutive_suppressions == 2
    d = tuc.decide_transient_ui_suppression(ctx, tuc.ARM_TREATMENT, detected=False)
    assert d.suppress is False and d.capped is False
    assert ctx.transient_ui_consecutive_suppressions == 0


def test_decide_suppression_control_and_off_never_suppress_or_touch_counter() -> None:
    for arm in (tuc.ARM_CONTROL, tuc.ARM_OFF):
        ctx = _ctx(workflow_run_id="wr_arm", preserve_transient_ui_capture=False)
        d = tuc.decide_transient_ui_suppression(ctx, arm, detected=True)
        assert d.suppress is False and d.capped is False
        assert ctx.transient_ui_consecutive_suppressions == 0
