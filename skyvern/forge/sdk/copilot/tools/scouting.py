from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import Any, cast
from urllib.parse import urlparse

import structlog

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.copilot.composition_browser_expressions import (
    scout_accessible_role_name_expression as _scout_accessible_role_name_expression,
)
from skyvern.forge.sdk.copilot.composition_evidence import (
    SCOUT_INTERACTION_EVIDENCE_TOOL,
    has_bounded_page_schema,
)
from skyvern.forge.sdk.copilot.enforcement import _RECENT_TOOL_OUTPUT_CHAR_CAP
from skyvern.forge.sdk.copilot.reached_download_target import (
    ReachedDownloadTarget,
)
from skyvern.forge.sdk.copilot.reached_download_target import (
    derive_from_navigation_targets as _derive_reached_download_from_nav_targets,
)
from skyvern.forge.sdk.copilot.reached_download_target import guidance_for as _reached_download_guidance_for
from skyvern.forge.sdk.copilot.runtime import (
    AgentContext,
    PendingBrowserInteractionObservation,
    ScoutedInteraction,
)

from ._shared import (
    _DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
    _append_flow_evidence,
    _composition_get_structured_evidence,
    _same_page_ignoring_fragment,
    _workflow_verification_evidence,
)

LOG = structlog.get_logger()


def _mark_page_inspected(ctx: AgentContext) -> None:
    ctx.post_budget_page_inspection_required = False
    ctx.post_budget_page_inspection_url = None
    ctx.post_budget_page_inspection_run_id = None


def _clear_pending_browser_interaction_observation(ctx: AgentContext) -> None:
    ctx.pending_browser_interaction_observation = None


def _mark_pending_browser_interaction_observation(ctx: AgentContext, *, tool_name: str, url: str) -> None:
    if not url.strip():
        _clear_pending_browser_interaction_observation(ctx)
        return
    ctx.pending_browser_interaction_observation = PendingBrowserInteractionObservation(
        tool_name=tool_name,
        url=url.strip(),
    )


def _consume_pending_browser_interaction_observation(
    ctx: AgentContext,
    *,
    current_url: str,
    evidence: dict[str, Any],
) -> bool:
    pending = ctx.pending_browser_interaction_observation
    if pending is None:
        return False
    _clear_pending_browser_interaction_observation(ctx)
    if not has_bounded_page_schema(evidence):
        return False
    if not _same_page_ignoring_fragment(pending.url, current_url):
        LOG.warning(
            "copilot_pending_browser_interaction_observation_page_mismatch",
            tool_name=pending.tool_name,
            pending_url=pending.url,
            current_url=current_url,
        )
        return False
    return True


_MAX_SCOUTED_INTERACTIONS = 60


async def _live_working_page_url(ctx: AgentContext) -> str | None:
    if not ctx.browser_session_id:
        return None
    try:
        browser_state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
            session_id=ctx.browser_session_id,
            organization_id=ctx.organization_id,
        )
        if not browser_state:
            return None
        page = await browser_state.get_or_create_page()
        return page.url if page else None
    except Exception:
        return None


async def _capture_scout_source_url(ctx: AgentContext) -> None:
    # Pre-action: a navigating click/Enter would leave only the destination URL, not the page the selector acted on.
    ctx.pending_scout_source_url = await _live_working_page_url(ctx)


def _consume_scout_source_url(ctx: AgentContext) -> str | None:
    source_url = ctx.pending_scout_source_url
    # Cleared unconditionally so a non-recording action can't bleed its source page into a later interaction.
    ctx.pending_scout_source_url = None
    return source_url


_ROLE_NAME_SELECTOR_RE = re.compile(r'^role=([a-zA-Z]+)(?:\[name="((?:[^"\\]|\\.)*)"\])?(.*)$')


def _selector_text(selector: Any) -> str:
    return selector.strip() if isinstance(selector, str) else ""


def _role_name_from_selector(selector: str | None) -> tuple[str, str] | None:
    """Parse the ``role=<role>[name="<name>"]`` form (ref_to_selector) — TIER 1, no browser read.

    Returns (role, accessible_name) when the selector is a plain role/name locator;
    None for bare CSS/xpath or when an engine chain (`>> nth=`) trails the role/name.
    """
    selector = _selector_text(selector)
    match = _ROLE_NAME_SELECTOR_RE.match(selector)
    if not match:
        return None
    role, raw_name, suffix = match.group(1), match.group(2), match.group(3)
    if suffix.strip():
        return None
    name = raw_name.replace('\\"', '"') if raw_name is not None else ""
    return role, name


async def _capture_accessible_role_name(ctx: AgentContext, selector: str | None) -> tuple[str, str] | None:
    """TIER 2: read the element's role/accessible name for a bare CSS/xpath selector.

    A failed read degrades gracefully to None so the selector-only auto-credit
    path (SKY-10712) stays intact.
    """
    selector = _selector_text(selector)
    if not selector:
        return None
    server = getattr(ctx, "discovery_mcp_server", None)
    if server is None:
        return None
    try:
        result = await asyncio.wait_for(
            server.call_internal_tool(
                "skyvern_evaluate",
                {"expression": _scout_accessible_role_name_expression(selector)},
            ),
            timeout=_DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    value = (result.get("data") or {}).get("result")
    if not isinstance(value, dict):
        return None
    role = str(value.get("role") or "").strip()
    name = str(value.get("accessible_name") or "").strip()
    if not role and not name:
        return None
    return role, name


async def _resolve_scout_role_name(
    ctx: AgentContext, selector: str | None, *, allow_browser_read: bool = True
) -> tuple[str, str]:
    """Resolve (role, accessible_name) for a scouted selector. TIER 1 parse first;
    TIER 2 browser read only for bare CSS/xpath. Always degrades to ("", "").

    ``allow_browser_read=False`` skips TIER 2 when the action navigated: a post-action
    read against the landing page would capture the wrong element's name, so the bare
    selector is kept verbatim (the synthesizer prefers it anyway)."""
    selector = _selector_text(selector)
    if not selector:
        return "", ""
    parsed = _role_name_from_selector(selector)
    if parsed is not None:
        return parsed
    if not allow_browser_read:
        return "", ""
    captured = await _capture_accessible_role_name(ctx, selector)
    if captured is not None:
        return captured
    return "", ""


def _record_scouted_interaction(
    ctx: AgentContext,
    *,
    tool_name: str,
    selector: str | None = None,
    source_url: str | None = None,
    value: str = "",
    typed_value: str = "",
    key: str = "",
    typed_length: int = 0,
    role: str = "",
    accessible_name: str = "",
    credential_id: str = "",
    credential_field: str = "",
    credential_name: str = "",
) -> None:
    selector = _selector_text(selector)
    # press_key may be page-level, so it is recorded by key even with no selector; other tools require one.
    if tool_name != "press_key" and not selector:
        return
    _reset_evaluate_actionable_tracker(ctx)
    artifact: ScoutedInteraction = {"tool_name": tool_name}
    if selector:
        artifact["selector"] = selector
    if source_url and source_url.strip():
        artifact["source_url"] = source_url.strip()
    if value:
        artifact["value"] = value
    if typed_value:
        artifact["typed_value"] = typed_value
    if key:
        artifact["key"] = key
    if typed_length:
        artifact["typed_length"] = typed_length
    if role:
        artifact["role"] = role
    if accessible_name:
        artifact["accessible_name"] = accessible_name
    if credential_id:
        artifact["credential_id"] = credential_id
    if credential_field:
        artifact["credential_field"] = credential_field
    if credential_name:
        artifact["credential_name"] = credential_name
    interactions = [
        item
        for item in ctx.scouted_interactions
        if not (
            item.get("tool_name") == artifact["tool_name"]
            and item.get("selector") == artifact.get("selector")
            and item.get("source_url") == artifact.get("source_url")
            and item.get("credential_field") == artifact.get("credential_field")
        )
    ]
    interactions.append(artifact)
    ctx.scouted_interactions = interactions[-_MAX_SCOUTED_INTERACTIONS:]

    trajectory = list(ctx.scout_trajectory)
    trajectory_artifact = cast(ScoutedInteraction, artifact.copy())
    trajectory_artifact["trajectory_index"] = len(trajectory)
    trajectory.append(trajectory_artifact)
    ctx.scout_trajectory = trajectory[-_MAX_SCOUTED_INTERACTIONS:]

    LOG.info(
        "copilot_scout_interaction_captured",
        tool_name=tool_name,
        selector=selector or None,
        source_url=artifact.get("source_url"),
        role=role or None,
        total_scouted_interactions=len(ctx.scouted_interactions),
        total_scout_trajectory=len(ctx.scout_trajectory),
    )


_ACT_OBSERVE_TOOLS = frozenset({"click"})


async def _scout_act_observe_page_evidence(ctx: AgentContext, *, url: str) -> dict[str, Any] | None:
    """Run the bounded page-side extractor right after a scout interaction.

    Degrades to None on timeout, error, or a hollow parse so the interaction
    result is never blocked or failed by capture problems."""
    if getattr(ctx, "discovery_mcp_server", None) is None:
        return None
    timeout_seconds = settings.COPILOT_SCOUT_ACT_OBSERVE_TIMEOUT_SECONDS
    started = time.monotonic()
    parsed: dict[str, Any] | None = None
    try:
        parsed = await _composition_get_structured_evidence(
            ctx, inspected_url=url, current_url=url, timeout_seconds=timeout_seconds
        )
        if parsed is not None and has_bounded_page_schema(parsed):
            outcome = "attached"
        elif parsed is not None:
            outcome = "hollow"
            parsed = None
        else:
            outcome = "timeout" if time.monotonic() - started >= timeout_seconds else "error"
    except Exception:
        parsed = None
        outcome = "error"
    LOG.info(
        "copilot_scout_act_observe",
        outcome=outcome,
        duration_ms=int((time.monotonic() - started) * 1000),
        url=url,
    )
    return parsed


async def _register_scout_interaction_observation(
    ctx: AgentContext, *, tool_name: str, selector: str | None, source_url: str | None, url: str
) -> tuple[int | None, dict[str, Any] | None]:
    # A successful scout interaction reaches the post-action page; record it as an
    # interaction-reached observation so a click-reached block can be authored
    # against it without a separate inspect_page_for_composition.
    selector = _selector_text(selector)
    if not selector or not url:
        return None, None
    evidence: dict[str, Any] = {
        "inspected_url": url,
        "current_url": url,
        "source_tool": SCOUT_INTERACTION_EVIDENCE_TOOL,
        "interaction_tool": tool_name,
        "interaction_selector": selector,
    }
    if source_url and source_url.strip():
        evidence["interaction_source_url"] = source_url.strip()
    page_evidence: dict[str, Any] | None = None
    if tool_name in _ACT_OBSERVE_TOOLS:
        parsed = await _scout_act_observe_page_evidence(ctx, url=url)
        if parsed is not None:
            # Identity keys overwrite the parsed packet so the entry stays a
            # scout_interaction observation, with the schema merged before append.
            evidence = {**parsed, **evidence}
            page_evidence = evidence
            # The schema is already attached; leaving the marker set would let a
            # later evaluate/inspect mint a second interaction credit for one click.
            _clear_pending_browser_interaction_observation(ctx)
    step = _append_flow_evidence(ctx, evidence, reached_via="interaction")
    return step, page_evidence


_PAGE_SUMMARY_TEXT_CAP = 80
_PAGE_SUMMARY_MAX_FIELDS = 8
_PAGE_SUMMARY_MAX_SUBMITS = 4
_PAGE_SUMMARY_MAX_NAV_TEXTS = 8
_PAGE_SUMMARY_MAX_DISMISS_TEXTS = 4


def _summary_text(value: Any) -> str:
    return value.strip()[:_PAGE_SUMMARY_TEXT_CAP] if isinstance(value, str) else ""


def _summary_field_name(field: dict[str, Any]) -> str:
    for key in ("label", "name", "placeholder", "id"):
        text = _summary_text(field.get(key))
        if text:
            return text
    return ""


def _build_scout_page_summary(evidence: dict[str, Any]) -> dict[str, Any]:
    forms_summary: list[dict[str, Any]] = []
    for form in evidence.get("forms") or []:
        if not isinstance(form, dict):
            continue
        fields = [field for field in form.get("fields") or [] if isinstance(field, dict)]
        submits = [control for control in form.get("submit_controls") or [] if isinstance(control, dict)]
        forms_summary.append(
            {
                "field_count": len(fields),
                "fields": [
                    name for name in (_summary_field_name(field) for field in fields[:_PAGE_SUMMARY_MAX_FIELDS]) if name
                ],
                "submit_controls": [
                    text
                    for text in (
                        _summary_text(control.get("text") or control.get("value"))
                        for control in submits[:_PAGE_SUMMARY_MAX_SUBMITS]
                    )
                    if text
                ],
            }
        )
    nav_targets = [target for target in evidence.get("navigation_targets") or [] if isinstance(target, dict)]
    dismiss_texts: list[str] = []
    for overlay in evidence.get("modal_overlays") or []:
        if not isinstance(overlay, dict):
            continue
        for control in overlay.get("dismiss_controls") or []:
            if len(dismiss_texts) >= _PAGE_SUMMARY_MAX_DISMISS_TEXTS:
                break
            if not isinstance(control, dict):
                continue
            text = _summary_text(control.get("text") or control.get("aria_label") or control.get("title"))
            if text:
                dismiss_texts.append(text)
    challenge_state = evidence.get("challenge_state")
    challenge_detected = bool(evidence.get("challenge_controls")) or (
        isinstance(challenge_state, dict) and challenge_state.get("detected") is True
    )
    return {
        "page_title": _summary_text(evidence.get("page_title")),
        "forms": forms_summary,
        "navigation_target_count": len(nav_targets),
        "navigation_targets": [
            text
            for text in (_summary_text(target.get("text")) for target in nav_targets[:_PAGE_SUMMARY_MAX_NAV_TEXTS])
            if text
        ],
        "result_container_count": len(evidence.get("result_containers") or []),
        "challenge_detected": challenge_detected,
        "modal_dismiss_controls": dismiss_texts,
    }


def _shed_scout_page_summary_section(summary: dict[str, Any]) -> bool:
    """Drop one summary section, in fixed priority order; False when nothing is left to shed."""
    if summary.get("navigation_targets"):
        summary["navigation_targets"] = []
        return True
    forms = [form for form in summary.get("forms") or [] if isinstance(form, dict)]
    for form in forms[1:]:
        if form.get("fields"):
            form["fields"] = []
            return True
    if summary.get("modal_dismiss_controls"):
        summary["modal_dismiss_controls"] = []
        return True
    for form in forms[1:]:
        if form.get("submit_controls"):
            form["submit_controls"] = []
            return True
    if forms and forms[0].get("fields"):
        fields = forms[0]["fields"]
        forms[0]["fields"] = fields[: len(fields) // 2] if len(fields) > 2 else []
        return True
    if forms and forms[0].get("submit_controls"):
        forms[0]["submit_controls"] = []
        return True
    if len(forms) > 1:
        summary["forms"] = forms[:1]
        return True
    return False


def _attach_scout_page_summary(result: dict[str, Any], page_evidence: dict[str, Any]) -> None:
    """Attach a compact page summary at result["data"]["page"], keeping the whole
    serialized result under the recent-output pruner cap by shedding sections —
    never by slicing the serialized JSON."""
    data = result.get("data")
    if not isinstance(data, dict):
        return
    try:
        summary = _build_scout_page_summary(page_evidence)
        data["page"] = summary
        while len(json.dumps(result)) > _RECENT_TOOL_OUTPUT_CHAR_CAP:
            if not _shed_scout_page_summary_section(summary):
                data.pop("page", None)
                return
    except Exception:
        data.pop("page", None)
        LOG.warning("copilot_scout_act_observe_summary_failed", exc_info=True)


_EVALUATE_ACTIONABLE_MAX_TARGETS = 4

_EVALUATE_ACTIONABLE_ACT_INSTRUCTION = (
    "This page already exposes actionable targets; click the intended one rather than re-evaluating."
)


def _reset_evaluate_actionable_tracker(ctx: AgentContext) -> None:
    ctx.last_evaluate_actionable_signature = None
    ctx.last_evaluate_actionable_url = None


def _actionable_target_identities(evidence: dict[str, Any]) -> list[tuple[str, str]]:
    identities: list[tuple[str, str]] = []

    def add(control: Any) -> None:
        if not isinstance(control, dict):
            return
        selector = _summary_text(control.get("selector"))
        text = _summary_text(control.get("text") or control.get("value") or control.get("aria_label"))
        if selector or text:
            identities.append((selector, text))

    for form in evidence.get("forms") or []:
        if not isinstance(form, dict):
            continue
        for field_entry in form.get("fields") or []:
            add(field_entry)
        for control in form.get("submit_controls") or []:
            add(control)
    for target in evidence.get("navigation_targets") or []:
        add(target)
    for container in evidence.get("result_containers") or []:
        add(container)
    for overlay in evidence.get("modal_overlays") or []:
        if not isinstance(overlay, dict):
            continue
        for control in overlay.get("dismiss_controls") or []:
            add(control)
    return identities


def _actionable_target_signature(identities: list[tuple[str, str]]) -> str:
    canonical = json.dumps(sorted(identities), separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _actionable_targets_for_result(identities: list[tuple[str, str]]) -> list[dict[str, str]]:
    targets: list[dict[str, str]] = []
    for selector, text in identities[:_EVALUATE_ACTIONABLE_MAX_TARGETS]:
        entry = {key: value for key, value in {"selector": selector, "text": text}.items() if value}
        if entry:
            targets.append(entry)
    return targets


# Verbs that imply an irreversible or money-moving side effect — never auto-clicked.
_AUTO_ACT_HIGH_TIER_VERBS = frozenset(
    {
        "pay",
        "payment",
        "purchase",
        "buy",
        "order",
        "place order",
        "checkout",
        "delete",
        "remove",
        "transfer",
        "send",
        "submit payment",
        "confirm payment",
        "wire",
        "withdraw",
        "cancel",
    }
)


def _auto_act_is_high_tier_label(*labels: Any) -> bool:
    for label in labels:
        if not isinstance(label, str):
            continue
        normalized = label.strip().lower()
        if not normalized:
            continue
        if any(verb in normalized for verb in _AUTO_ACT_HIGH_TIER_VERBS):
            return True
    return False


def _auto_act_href_is_navigation(href: Any) -> bool:
    if not isinstance(href, str):
        return False
    candidate = href.strip()
    if not candidate or candidate.startswith("#"):
        return False
    lowered = candidate.lower()
    if lowered.startswith(("javascript:", "mailto:", "tel:")):
        return False
    if lowered.startswith(("http://", "https://")):
        return True
    return not lowered.startswith(("data:", "blob:"))


def _auto_act_candidate(parsed: dict[str, Any]) -> dict[str, Any] | None:
    """Return the single unambiguous, low-tier nav link to auto-click, or None.

    Eligible only from navigation_targets (`<a href>` with a real http/https/relative
    href and non-empty text); form submit_controls, form fields, result containers, and
    modal dismiss controls are excluded. Form submits are never candidates because the
    structured-evidence producer cannot reliably distinguish a writing submit from a
    bare default-submit button, so the whole form-submit class is dropped for safety.
    Money-moving / destructive verbs are dropped. Exactly one survivor ⇒ act; zero or
    more than one ⇒ None."""
    candidates: list[dict[str, Any]] = []

    for target in parsed.get("navigation_targets") or []:
        if not isinstance(target, dict):
            continue
        selector = _summary_text(target.get("selector"))
        if not selector or target.get("disabled") is True:
            continue
        if not _auto_act_href_is_navigation(target.get("href")):
            continue
        text = _summary_text(target.get("text"))
        if not text:
            continue
        if _auto_act_is_high_tier_label(text, target.get("name"), target.get("id")):
            continue
        candidates.append({"selector": selector, "text": text})

    if len(candidates) != 1:
        return None
    return candidates[0]


_EVALUATE_STEER_SHED_MARKER = "[omitted on repeat — act on the named target instead of re-reading]"

# Keys the steer must never shed: navigation/identity context plus the steer's own output.
_EVALUATE_STEER_ESSENTIAL_KEYS = frozenset(
    {"url", "title", "observation_step", "actionable_targets", "next_action", "next_action_reason"}
)

# Nested bulky subfields inside an evaluate `result` dict (the raw page payload).
_EVALUATE_STEER_NESTED_BULKY_KEYS = ("html", "outerHTML", "innerHTML", "body", "bodyText", "text", "buttons")


def _serialized_len(value: Any) -> int:
    try:
        return len(json.dumps(value, default=str))
    except Exception:
        return len(str(value))


def _largest_non_essential_data_key(data: dict[str, Any]) -> str | None:
    candidates = [
        (key, _serialized_len(value))
        for key, value in data.items()
        if key not in _EVALUATE_STEER_ESSENTIAL_KEYS and value != _EVALUATE_STEER_SHED_MARKER
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[1], reverse=True)
    return candidates[0][0]


def _fit_evaluate_steer_under_cap(result: dict[str, Any], data: dict[str, Any], *, is_repeat: bool) -> None:
    """Keep the serialized result under the recent-output cap without ever head-slicing it.

    A first evaluate is reconnaissance: if naming targets alone pushes it over cap, drop only the
    advisory `actionable_targets` and leave the raw page (`data["result"]`) for the model to read.
    A repeat is an imperative steer: the model already saw the page on evaluate #1, so shed the bulky
    non-essential payload (the nested raw fields of `result`, then `result`/any large key wholesale,
    replaced by a short marker) while always keeping `next_action`, its reason, and >=1 target."""

    def over_cap() -> bool:
        return len(json.dumps(result, default=str)) > _RECENT_TOOL_OUTPUT_CHAR_CAP

    if not over_cap():
        return
    if not is_repeat:
        data.pop("actionable_targets", None)
        return
    nested = data.get("result")
    if isinstance(nested, dict):
        for key in _EVALUATE_STEER_NESTED_BULKY_KEYS:
            if key in nested and nested[key] != _EVALUATE_STEER_SHED_MARKER:
                nested[key] = _EVALUATE_STEER_SHED_MARKER
                if not over_cap():
                    return
    while over_cap():
        largest_key = _largest_non_essential_data_key(data)
        if largest_key is None:
            break
        data[largest_key] = _EVALUATE_STEER_SHED_MARKER
        if not over_cap():
            return
    targets = data.get("actionable_targets")
    while isinstance(targets, list) and len(targets) > 1 and over_cap():
        targets.pop()


def _auto_act_essential_keys() -> frozenset[str]:
    return _EVALUATE_STEER_ESSENTIAL_KEYS | {"auto_acted", "page"}


async def _auto_act_on_repeat(ctx: AgentContext, result: dict[str, Any], *, url: str, target: dict[str, Any]) -> bool:
    """Issue the in-process click for the single unambiguous target and reshape the result.

    Returns True when the click landed and the result was reshaped to report it; False
    degrades the caller to the advisory steer (next_action/actionable_targets left intact)."""
    data = result.get("data")
    if not isinstance(data, dict):
        return False
    server = getattr(ctx, "discovery_mcp_server", None)
    if server is None:
        return False
    selector = target["selector"]
    pre_url = await _live_working_page_url(ctx) or url
    try:
        click_result = await asyncio.wait_for(
            server.call_internal_tool("skyvern_click", {"selector": selector, "selector_mode": "direct"}),
            timeout=settings.COPILOT_SCOUT_ACT_OBSERVE_TIMEOUT_SECONDS,
        )
    except Exception:
        LOG.warning("copilot_evaluate_auto_act_failed", url=pre_url, selector=selector, exc_info=True)
        return False
    if not isinstance(click_result, dict) or not click_result.get("ok"):
        LOG.warning(
            "copilot_evaluate_auto_act_failed",
            url=pre_url,
            selector=selector,
            error=(click_result or {}).get("error") if isinstance(click_result, dict) else None,
        )
        return False

    post_url = await _live_working_page_url(ctx) or url
    post_evidence = await _scout_act_observe_page_evidence(ctx, url=post_url)
    _record_scouted_interaction(ctx, tool_name="click", selector=selector, source_url=pre_url)

    for key in ("next_action", "next_action_reason", "actionable_targets"):
        data.pop(key, None)
    data["auto_acted"] = {"tool": "click", "selector": selector, "text": target.get("text", "")}
    if post_evidence is None:
        data["auto_acted"]["note"] = "clicked; post-click page evidence unavailable"
    else:
        data["page"] = _build_scout_page_summary(post_evidence)
    essential = _auto_act_essential_keys()
    while len(json.dumps(result, default=str)) > _RECENT_TOOL_OUTPUT_CHAR_CAP:
        largest = max(
            (
                (key, _serialized_len(value))
                for key, value in data.items()
                if key not in essential and value != _EVALUATE_STEER_SHED_MARKER
            ),
            key=lambda item: item[1],
            default=None,
        )
        if largest is None:
            page = data.get("page")
            if not isinstance(page, dict) or not _shed_scout_page_summary_section(page):
                break
            continue
        data[largest[0]] = _EVALUATE_STEER_SHED_MARKER
    LOG.info("copilot_evaluate_auto_acted", url=post_url, selector=selector)
    return True


class _UnsetEvidence:
    pass


_EVIDENCE_UNSET = _UnsetEvidence()


async def _maybe_steer_evaluate_to_action(
    ctx: AgentContext,
    result: dict[str, Any],
    *,
    url: str,
    page_evidence: dict[str, Any] | None | _UnsetEvidence = _EVIDENCE_UNSET,
) -> bool:
    data = result.get("data")
    if not isinstance(data, dict):
        return False
    try:
        parsed = (
            await _scout_act_observe_page_evidence(ctx, url=url)
            if isinstance(page_evidence, _UnsetEvidence)
            else page_evidence
        )
        if parsed is None or not has_bounded_page_schema(parsed):
            _reset_evaluate_actionable_tracker(ctx)
            return False
        identities = _actionable_target_identities(parsed)
        if not identities:
            _reset_evaluate_actionable_tracker(ctx)
            return False
        signature = _actionable_target_signature(identities)
        # Strict full-URL match (fragment included): on an SPA a hash-route change
        # is a navigation, so a differing fragment must read as a different page.
        is_repeat = ctx.last_evaluate_actionable_signature == signature and ctx.last_evaluate_actionable_url == url
        ctx.last_evaluate_actionable_signature = signature
        ctx.last_evaluate_actionable_url = url
        if signature != ctx.last_auto_acted_signature and ctx.last_auto_acted_signature is not None:
            ctx.last_auto_acted_signature = None
        targets = _actionable_targets_for_result(identities)
        if (
            settings.COPILOT_EVALUATE_AUTO_ACT_ON_REPEAT_ENABLED
            and is_repeat
            and ctx.last_auto_acted_signature != signature
        ):
            candidate = _auto_act_candidate(parsed)
            if candidate is not None:
                ctx.last_auto_acted_signature = signature
                if await _auto_act_on_repeat(ctx, result, url=url, target=candidate):
                    LOG.info("copilot_evaluate_actionable_target_steer", url=url, is_repeat=True, steered=True)
                    return True
        if targets:
            data["actionable_targets"] = targets
            if is_repeat:
                data["next_action"] = "click"
                data["next_action_reason"] = _EVALUATE_ACTIONABLE_ACT_INSTRUCTION
            _fit_evaluate_steer_under_cap(result, data, is_repeat=is_repeat)
        LOG.info(
            "copilot_evaluate_actionable_target_steer",
            url=url,
            actionable_target_count=len(identities),
            is_repeat=is_repeat,
            steered=is_repeat and bool(targets),
        )
    except Exception:
        data.pop("actionable_targets", None)
        data.pop("next_action", None)
        data.pop("next_action_reason", None)
        _reset_evaluate_actionable_tracker(ctx)
        LOG.warning("copilot_evaluate_actionable_target_steer_failed", exc_info=True)
    return False


def _register_reached_download_scout_interaction(ctx: AgentContext, target: ReachedDownloadTarget, *, url: str) -> None:
    """Record the evaluate-resolved download affordance as a scout_interaction observation.

    The scout-act download gate is cleared by a scout_interaction this turn, but the reached-download
    target is resolved on the evaluate post-hook (source_tool="evaluate"). Registering the affordance
    here unifies the two: the same evaluate call that feeds the synthesizer also clears the gate, so
    obeying the scout-act steering is sufficient and the gate cannot loop on a scouted download.
    """
    selector = target.selector.strip()
    if not selector or not url.strip():
        return
    _append_flow_evidence(
        ctx,
        {
            "inspected_url": url,
            "current_url": url,
            "source_tool": SCOUT_INTERACTION_EVIDENCE_TOOL,
            "interaction_tool": "evaluate",
            "interaction_selector": selector,
            "download_kind": target.download_kind,
        },
        reached_via="interaction",
    )


async def _maybe_attach_reached_download_target(
    ctx: AgentContext,
    result: dict[str, Any],
    *,
    url: str,
    page_evidence: dict[str, Any] | None | _UnsetEvidence = _EVIDENCE_UNSET,
) -> None:
    """Attach a typed reached-download target + guidance when the page exposes exactly one same-host
    download affordance, matched on the captured selector (never URL — a download does not change the SPA URL)."""
    if not settings.COPILOT_REACHED_DOWNLOAD_TARGET_AUTHOR_STEER_ENABLED:
        return
    data = result.get("data")
    if not isinstance(data, dict):
        return
    try:
        parsed = (
            await _scout_act_observe_page_evidence(ctx, url=url)
            if isinstance(page_evidence, _UnsetEvidence)
            else page_evidence
        )
        if parsed is None:
            return
        target = _derive_reached_download_from_nav_targets(parsed.get("navigation_targets"))
        if target is None:
            return
        data["reached_download_target"] = target.to_dict()
        data["reached_download_guidance"] = _reached_download_guidance_for(target)
        if settings.COPILOT_DOWNLOAD_RUNG_SYNTHESIS_ENABLED and not target.already_registered:
            # The pure synthesizer compiles the terminal expect_download step from this typed object.
            ctx.reached_download_target = target
            if ctx.synthesized_block_offered and not ctx.update_workflow_called:
                # The prompt-side offer latched before this download target resolved, so it rendered the
                # non-download idiom. Reopen the latch once so the post-turn fallback re-fires carrying it.
                ctx.synthesized_block_offered = False
                LOG.info("copilot_synthesized_block_offer_latch_reset_for_download", url=url)
        if settings.COPILOT_DOWNLOAD_SCOUT_ACT_REQUIRED_ENABLED and not target.already_registered:
            _register_reached_download_scout_interaction(ctx, target, url=url)
        LOG.info(
            "copilot_reached_download_target_steer",
            url=url,
            download_kind=target.download_kind,
            already_registered=target.already_registered,
        )
    except Exception:
        data.pop("reached_download_target", None)
        data.pop("reached_download_guidance", None)
        LOG.warning("copilot_reached_download_target_steer_failed", exc_info=True)


async def _steer_evaluate_result(ctx: AgentContext, result: dict[str, Any], *, url: str) -> None:
    # Observe the bounded page evidence once and feed both evaluate steers; re-observe for the
    # download steer only when the actionable steer auto-acted and may have changed the page.
    if not isinstance(result.get("data"), dict):
        return
    page_evidence = await _scout_act_observe_page_evidence(ctx, url=url)
    acted = await _maybe_steer_evaluate_to_action(ctx, result, url=url, page_evidence=page_evidence)
    await _maybe_attach_reached_download_target(
        ctx, result, url=url, page_evidence=_EVIDENCE_UNSET if acted else page_evidence
    )


def _mark_post_run_page_observed(ctx: AgentContext, *, source_tool: str, url: str) -> None:
    run_id = getattr(ctx, "last_run_blocks_workflow_run_id", None)
    if not isinstance(run_id, str) or not run_id:
        return
    ctx.post_run_page_observation_tool = source_tool
    ctx.post_run_page_observation_url = url
    ctx.post_run_page_observation_workflow_run_id = run_id
    ctx.post_run_page_observation_after_failed_test = getattr(ctx, "last_test_ok", None) is False
    evidence = _workflow_verification_evidence(ctx)
    evidence.live_page_state_verified = True
    evidence.verified_from_current_browser_state = True
    evidence.workflow_run_id = run_id
    if url:
        evidence.current_url = url
        evidence.current_url_observed_after_workflow_run = True
        evidence.current_url_may_encode_runtime_state = bool(urlparse(url).query)
