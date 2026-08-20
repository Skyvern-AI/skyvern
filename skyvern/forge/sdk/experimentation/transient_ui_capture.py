from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.experimentation.providers import NoOpExperimentationProvider

LOG = structlog.get_logger()

PRESERVE_TRANSIENT_UI_CAPTURE_FLAG = "PRESERVE_TRANSIENT_UI_CAPTURE"

ARM_TREATMENT = "treatment"
ARM_CONTROL = "control"
ARM_OFF = "off"

# A trigger that stays aria-expanded=true (e.g. a stale/undismissed combobox) would otherwise hold
# every agent-step capture at one viewport for the rest of the run. Allow at most this many
# CONSECUTIVE suppressing captures — enough for the post-action screenshot plus the next agent-step
# scrape — then fall back to legacy scrolling until a scrape sees no qualifying popup (which resets
# the run's consecutive counter).
MAX_CONSECUTIVE_TRANSIENT_UI_SUPPRESSIONS = 2

# role / aria-haspopup are page-controlled attribute values. domUtils.js already returns only the
# matched token, but the capture site re-validates against these allowlists so arbitrary
# attacker-authored text can never reach telemetry even if the detector return shape changes.
TRANSIENT_UI_ROLE_TELEMETRY_ALLOWLIST = frozenset({"combobox"})
TRANSIENT_UI_HASPOPUP_TELEMETRY_ALLOWLIST = frozenset({"true", "listbox", "menu", "dialog", "grid", "tree"})


async def resolve_transient_ui_capture_arm(
    context: skyvern_context.SkyvernContext,
    *,
    distinct_id: str | None = None,
    organization_id: str | None = None,
    workflow_permanent_id: str | None = None,
) -> None:
    """Resolve the PRESERVE_TRANSIENT_UI_CAPTURE experiment once at execution start and pin the
    tri-state arm on the context. Idempotent: after the first resolution — True, False, or None
    (undefined flag, no provider, missing run identity, or resolver error) — later calls return
    immediately, so a TTL expiry or a mid-run flag ramp cannot flip the arm and the provider is
    queried at most once per run; the capture hot paths only read the cached value. Off is the
    safe default.

    An execution-boundary caller whose context does not yet carry full run identity — notably an
    inline child workflow, whose scoped context has no workflow_run_id/organization_id — may pass
    distinct_id/organization_id/workflow_permanent_id explicitly so the child is attributed to its
    own run rather than the parent's; otherwise they are read from the context.
    """
    if context.preserve_transient_ui_capture_resolved:
        return
    provider = app.EXPERIMENTATION_PROVIDER
    if isinstance(provider, NoOpExperimentationProvider):
        # No experimentation configured (OSS default) -> off, and never query the provider.
        context.preserve_transient_ui_capture_resolved = True
        return
    resolved_distinct_id = (
        distinct_id or context.workflow_run_id or context.task_id or context.task_v2_id or context.run_id
    )
    if not resolved_distinct_id:
        context.preserve_transient_ui_capture_resolved = True
        return
    # Single-flight the provider query: concurrent first-resolvers sharing this context must not
    # both call the provider. Re-check inside the lock in case another coroutine pinned it while we
    # waited (the fast-path check above is unlocked to keep already-resolved contexts lock-free).
    async with context.preserve_transient_ui_capture_lock:
        if context.preserve_transient_ui_capture_resolved:
            return
        try:
            context.preserve_transient_ui_capture = await provider.resolve_feature_flag_cached(
                PRESERVE_TRANSIENT_UI_CAPTURE_FLAG,
                resolved_distinct_id,
                properties={
                    "organization_id": organization_id or context.organization_id,
                    "workflow_permanent_id": workflow_permanent_id or context.workflow_permanent_id or "not_workflow",
                },
            )
        except Exception:
            LOG.warning("Failed to resolve PRESERVE_TRANSIENT_UI_CAPTURE; defaulting to off/unenrolled", exc_info=True)
            context.preserve_transient_ui_capture = None
        context.preserve_transient_ui_capture_resolved = True


@dataclass(frozen=True)
class TransientUiSuppressionDecision:
    suppress: bool
    capped: bool


def decide_transient_ui_suppression(
    context: skyvern_context.SkyvernContext | None, arm: str, *, detected: bool
) -> TransientUiSuppressionDecision:
    """Apply the per-run consecutive-suppression cap for one agent-step capture and update the run
    counter. ``detected`` is whether a qualifying popup was found on THIS capture. Only the treatment
    arm suppresses or touches the counter (control/off shadow-detect but never suppress). The agent-
    step scrape and the post-action screenshot both call this so their consecutive suppressions count
    against ONE budget: suppress until the cap, then fall back to legacy scrolling, and reset the
    counter as soon as a capture sees no qualifying popup."""
    if arm != ARM_TREATMENT:
        return TransientUiSuppressionDecision(suppress=False, capped=False)
    if not detected:
        if context is not None:
            context.transient_ui_consecutive_suppressions = 0
        return TransientUiSuppressionDecision(suppress=False, capped=False)
    if context is None or context.transient_ui_consecutive_suppressions < MAX_CONSECUTIVE_TRANSIENT_UI_SUPPRESSIONS:
        if context is not None:
            context.transient_ui_consecutive_suppressions += 1
        return TransientUiSuppressionDecision(suppress=True, capped=False)
    return TransientUiSuppressionDecision(suppress=False, capped=True)


def emit_transient_ui_popup_telemetry(span: Any, popup_trigger: dict) -> None:
    """Emit bounded popup attributes to ``span``. role / aria-haspopup are page-controlled, so only
    an allowlisted matched token is emitted (a non-allowlisted value is omitted); controls-resolved
    is a bounded boolean so the no-target portal fallback (the main false-positive source) is
    measurable. Shared by both capture sites so telemetry stays consistent."""
    role_val = popup_trigger.get("role")
    if isinstance(role_val, str) and role_val in TRANSIENT_UI_ROLE_TELEMETRY_ALLOWLIST:
        span.set_attribute("transient_ui_role", role_val)
    haspopup_val = popup_trigger.get("hasPopup")
    if isinstance(haspopup_val, str) and haspopup_val in TRANSIENT_UI_HASPOPUP_TELEMETRY_ALLOWLIST:
        span.set_attribute("transient_ui_haspopup", haspopup_val)
    span.set_attribute("transient_ui_controls_resolved", bool(popup_trigger.get("controlsResolved")))


def transient_ui_capture_arm(context: skyvern_context.SkyvernContext | None) -> str:
    """Map the cached tri-state assignment to an arm: treatment (suppress scroll on detection),
    control (shadow-detect only), or off (undefined/no-provider/error/missing -> current behavior)."""
    if context is None:
        return ARM_OFF
    value = context.preserve_transient_ui_capture
    if value is True:
        return ARM_TREATMENT
    if value is False:
        return ARM_CONTROL
    return ARM_OFF
