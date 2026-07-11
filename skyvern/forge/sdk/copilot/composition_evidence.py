"""Build-time page evidence contract for Workflow Copilot composition."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse

import structlog
import yaml

try:
    from bs4 import BeautifulSoup  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - bs4 is a transitive dep but inspection degrades gracefully.
    BeautifulSoup = None  # type: ignore[assignment, misc]

from skyvern.config import settings
from skyvern.forge.sdk.copilot.build_phase import BuildPhase
from skyvern.forge.sdk.copilot.challenge_evidence import (
    CHALLENGE_EVIDENCE_SOURCE_KEY,
    CONSENT_OBSTRUCTION_KIND,
    ChallengeEvidenceSource,
    interactive_challenge_controls,
    vision_challenge_carrier,
)
from skyvern.forge.sdk.copilot.reached_download_target import (
    NAV_TARGET_DOWNLOAD_KIND_KEY,
    classify_download_affordance,
)
from skyvern.forge.sdk.copilot.verification_evidence import WorkflowVerificationEvidence
from skyvern.utils.yaml_loader import safe_load_no_dates

LOG = structlog.get_logger()

_BUILD_MODE_VALUES: frozenset[str] = frozenset({"build", "draft_only", "edit", "unknown"})
# Block types whose acted page, when no url is on the block, is the current
# frontier (observation of the page suffices). navigation without a url is the
# interaction-on-current-page case and is also frontier-anchored.
_FRONTIER_NO_URL_BLOCK_TYPES: frozenset[str] = frozenset({"login", "extraction", "validation"})
# No-url blocks that interact with the live page (not just read it): each runs the browser
# agent against the reached page, so they need the same observed-evidence floor as a no-url
# navigation, otherwise the agent can author an unobserved click/download/upload.
_INTERACTION_NO_URL_BLOCK_TYPES: frozenset[str] = frozenset({"navigation", "action", "file_download", "file_upload"})
# Navigation can legitimately split same-page form preparation and submission across
# blocks, so only no-url action/download/upload blocks force post-interaction refs.
_POST_INTERACTION_OBS_REQ_BLOCK_TYPES: frozenset[str] = frozenset({"action", "file_download", "file_upload"})
_GOTO_URL_BLOCK_TYPE = "goto_url"
_SCHEMA_EVIDENCE_TOOL = "inspect_page_for_composition"
_STRUCTURED_BROWSER_EVIDENCE_TOOLS: frozenset[str] = frozenset({"evaluate"})
_POST_RUN_CONTINUATION_EVIDENCE_TOOLS: frozenset[str] = frozenset({"inspect_page_for_composition", "evaluate"})
SCOUT_INTERACTION_EVIDENCE_TOOL = "scout_interaction"
_RESULT_CONTAINER_HINTS: frozenset[str] = frozenset({"result", "results", "record", "records", "row", "rows"})
_MAX_FORMS = 5
_MAX_FIELDS_PER_FORM = 20
_MAX_RESULT_CONTAINERS = 8
_MAX_KEY_VALUE_RELATIONS = 24
_MAX_TABLE_HEADERS = 12
_MAX_RESULT_SAMPLE_ROWS = 5
_MAX_NAVIGATION_TARGETS = 20
_MAX_SELECT_OPTIONS = 30
_MAX_CHALLENGE_CONTROLS = 8
_MAX_MODAL_OVERLAYS = 5
_MAX_MODAL_DISMISS_CONTROLS = 6
_MAX_PAGE_OBSTRUCTIONS = 5
_MAX_VISIBLE_CONTROLS = 6
_MAX_CLICKABLE_CONTROLS = 12
_MODAL_IDENTITY_PATTERNS: frozenset[str] = frozenset({"modal", "popup", "overlay", "dialog", "drawer", "lightbox"})
_MODAL_ROLE_VALUES: frozenset[str] = frozenset({"dialog", "alertdialog"})
_MODAL_DISMISS_HINTS: frozenset[str] = frozenset(
    {
        "cancel",
        "close",
        "dismiss",
        "got it",
        "no thanks",
        "not now",
        "skip",
    }
)
_MODAL_DISMISS_SYMBOLS: frozenset[str] = frozenset({"x", "\u00d7"})
_MAX_VISIBLE_TEXT_EXCERPT_CHARS = 3000
DOM_EVIDENCE_SOURCE = "dom_html"
DOM_STYLE_EVIDENCE_SOURCE = "dom_style"
SCREENSHOT_EVIDENCE_SOURCE = "screenshot"
VISION_EVIDENCE_SOURCE = "vision_summary"
_ANTI_BOT_PATTERNS = (
    "just a moment",
    "captcha",
    "challenge",
    "turnstile",
    "cf-turnstile",
    "human-verification",
    "human verification",
    "verify you are human",
    "access denied",
    "are you a robot",
)
_EMPTY_RESULT_TEXT_PATTERNS: frozenset[str] = frozenset(
    {
        "0 results",
        "no matching records",
        "no records found",
        "no results",
        "no results found",
        "nothing found",
    }
)
_MAX_VISUAL_SUMMARY_CHARS = 500
_MAX_VISUAL_OMISSIONS = 5
_ANTI_BOT_SCAN_BYTES = 250_000


class _PostRunCompositionContext(Protocol):
    composition_page_evidence: dict[str, Any] | None
    per_tool_budget_problem_block_labels: list[str]
    workflow_verification_evidence: WorkflowVerificationEvidence
    post_run_page_observation_after_failed_test: bool
    last_failure_category_top: str | None


def _bounded_string(value: Any, max_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    # Bounded evidence text is for Copilot-readable summaries; selectors are
    # built separately so whitespace-sensitive selector values are preserved.
    return " ".join(value.split())[:max_chars]


def _challenge_kind(indicators: list[str]) -> str:
    indicator_text = " ".join(indicators).lower()
    if "captcha" in indicator_text or "are you a robot" in indicator_text:
        return "captcha"
    if "access denied" in indicator_text:
        return "access_denied"
    if any(term in indicator_text for term in ("challenge", "human verification", "verify you are human")):
        return "human_verification"
    return "unknown" if indicators else "none"


def _challenge_state(
    indicators: list[str],
    *,
    source: str = DOM_EVIDENCE_SOURCE,
    gated_submit_controls: list[dict[str, Any]] | None = None,
    challenge_controls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    detected = bool(indicators)
    gated_controls = gated_submit_controls or []
    # Raw-HTML token hits only mark `detected` (triggering the visual fallback);
    # asserting human verification requires a rendered challenge control or a
    # later vision confirmation.
    semantic_challenge = bool(interactive_challenge_controls(challenge_controls))
    state = {
        "detected": detected,
        "kind": _challenge_kind(indicators),
        "source": source if detected else "",
        "indicators": indicators[:8],
        "requires_human_verification": semantic_challenge,
        "visual_location": "",
        "gates_submit_controls": bool(semantic_challenge and gated_controls),
        "gated_submit_controls": gated_controls[:5] if detected else [],
    }
    if semantic_challenge:
        state[CHALLENGE_EVIDENCE_SOURCE_KEY] = ChallengeEvidenceSource.CHALLENGE_STATE.value
    return state


def _control_disabled(node: Any) -> bool:
    if not hasattr(node, "has_attr"):
        return False
    return bool(
        node.has_attr("disabled")
        or str(node.get("aria-disabled") or "").strip().lower() == "true"
        or str(node.get("data-disabled") or "").strip().lower() == "true"
    )


def _gated_submit_controls(forms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    controls: list[dict[str, Any]] = []
    for form in forms:
        if not isinstance(form, dict):
            continue
        for control in form.get("submit_controls") or []:
            if not isinstance(control, dict) or control.get("disabled") is not True:
                continue
            controls.append(
                {
                    "text": _bounded_string(control.get("text") or control.get("value"), 120),
                    "id": _bounded_string(control.get("id"), 120),
                    "name": _bounded_string(control.get("name"), 120),
                    "selector": _bounded_string(control.get("selector"), 160),
                    "disabled": True,
                }
            )
    return controls[:5]


def _evidence_metadata(
    indicators: list[str] | None = None,
    *,
    forms: list[dict[str, Any]] | None = None,
    challenge_controls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    gated_controls = _gated_submit_controls(forms or [])
    return {
        "evidence_sources": [DOM_EVIDENCE_SOURCE],
        "screenshot_used": False,
        "visual_evidence_summary": "",
        "visual_evidence_omissions": [],
        "inspection_warnings": [],
        "challenge_state": _challenge_state(
            indicators or [],
            gated_submit_controls=gated_controls,
            challenge_controls=challenge_controls,
        ),
    }


def _bounded_visual_controls(values: Iterable[Any]) -> list[dict[str, Any]]:
    controls: list[dict[str, Any]] = []
    for value in values:
        if len(controls) >= _MAX_VISIBLE_CONTROLS:
            break
        text = _bounded_string(value, 120)
        if text:
            controls.append({"text": text})
    return controls


def _page_obstructions_from_visual_summary(visual_summary: dict[str, Any]) -> list[dict[str, Any]]:
    if visual_summary.get("page_obstruction_detected") is not True:
        return []
    obstruction: dict[str, Any] = {
        "kind": _bounded_string(visual_summary.get("obstruction_kind"), 80) or "visual_obstruction",
        "source": VISION_EVIDENCE_SOURCE,
        "visual_location": _bounded_string(visual_summary.get("obstruction_location"), 180),
        "visible_controls": _bounded_visual_controls(visual_summary.get("visible_dismiss_controls") or []),
    }
    if isinstance(visual_summary.get("underlying_page_blocked"), bool):
        obstruction["underlying_page_blocked"] = visual_summary["underlying_page_blocked"]
    return [
        {
            key: value
            for key, value in obstruction.items()
            if value or key in {"visible_controls", "underlying_page_blocked"}
        }
    ]


def page_evidence_needs_visual_fallback(evidence: dict[str, Any]) -> bool:
    """Return True when DOM evidence should be augmented with visual evidence."""

    challenge_state = evidence.get("challenge_state")
    if isinstance(challenge_state, dict) and challenge_state.get("detected") is True:
        return True
    visual_obstruction_candidates = evidence.get("visual_obstruction_candidates")
    if isinstance(visual_obstruction_candidates, list) and visual_obstruction_candidates:
        return True
    return bool(evidence.get("anti_bot_indicators") or evidence.get("challenge_controls"))


def _confirmed_visual_challenge(evidence: dict[str, Any], visual_summary: dict[str, Any]) -> bool:
    if visual_summary.get("challenge_detected") is not True:
        return False
    if interactive_challenge_controls(evidence.get("challenge_controls")):
        return True
    obstruction_kind = str(visual_summary.get("obstruction_kind") or "").strip().lower()
    return obstruction_kind != CONSENT_OBSTRUCTION_KIND


def merge_visual_composition_evidence(
    evidence: dict[str, Any],
    *,
    visual_summary: dict[str, Any] | None = None,
    visual_error: str | None = None,
) -> dict[str, Any]:
    """Add bounded screenshot/vision metadata to composition evidence.

    The raw screenshot stays tool-internal. This helper records only compact,
    typed facts that the main Copilot loop may use while composing.
    """

    merged = dict(evidence)
    sources = [str(source) for source in merged.get("evidence_sources") or [] if isinstance(source, str)]
    if SCREENSHOT_EVIDENCE_SOURCE not in sources:
        sources.append(SCREENSHOT_EVIDENCE_SOURCE)
    if visual_summary and VISION_EVIDENCE_SOURCE not in sources:
        sources.append(VISION_EVIDENCE_SOURCE)
    merged["evidence_sources"] = sources
    merged["screenshot_used"] = True

    omissions = [
        _bounded_string(item, 160)
        for item in (merged.get("visual_evidence_omissions") or [])
        if _bounded_string(item, 160)
    ][:_MAX_VISUAL_OMISSIONS]
    if visual_error:
        omissions.append(_bounded_string(f"visual_summary_error: {visual_error}", 160))

    if isinstance(visual_summary, dict):
        summary = _bounded_string(visual_summary.get("summary"), _MAX_VISUAL_SUMMARY_CHARS)
        if summary:
            merged["visual_evidence_summary"] = summary
        for item in visual_summary.get("omissions") or []:
            bounded = _bounded_string(item, 160)
            if bounded:
                omissions.append(bounded)
        challenge_state = dict(merged.get("challenge_state") or {})
        challenge_confirmed = _confirmed_visual_challenge(evidence, visual_summary)
        if vision_challenge_carrier(visual_summary):
            challenge_state.setdefault(CHALLENGE_EVIDENCE_SOURCE_KEY, ChallengeEvidenceSource.VISION.value)
        if challenge_confirmed:
            challenge_state["detected"] = True
            challenge_state["requires_human_verification"] = True
            challenge_state["source"] = (
                "dom+screenshot" if challenge_state.get("source") else SCREENSHOT_EVIDENCE_SOURCE
            )
        if challenge_confirmed or challenge_state.get("detected") is True:
            challenge_kind = _bounded_string(visual_summary.get("challenge_kind"), 80)
            if challenge_kind:
                challenge_state["kind"] = challenge_kind
            challenge_location = _bounded_string(visual_summary.get("challenge_location"), 180)
            if challenge_location:
                challenge_state["visual_location"] = challenge_location
        if visual_summary.get("submit_blocked") is True and challenge_confirmed:
            challenge_state["gates_submit_controls"] = True
        visual_blocked_controls = (
            [
                {
                    "text": _bounded_string(item, 120),
                    "disabled": True,
                }
                for item in visual_summary.get("blocked_submit_controls") or []
                if _bounded_string(item, 120)
            ]
            if challenge_confirmed
            else []
        )
        if visual_blocked_controls:
            existing_controls = [
                item for item in challenge_state.get("gated_submit_controls") or [] if isinstance(item, dict)
            ]
            challenge_state["gated_submit_controls"] = (existing_controls + visual_blocked_controls)[:5]
        merged["challenge_state"] = challenge_state
        visual_obstructions = _page_obstructions_from_visual_summary(visual_summary)
        if visual_obstructions:
            existing_obstructions = [item for item in merged.get("page_obstructions") or [] if isinstance(item, dict)]
            merged["page_obstructions"] = (existing_obstructions + visual_obstructions)[:_MAX_PAGE_OBSTRUCTIONS]
        # Empty-page classification is visual-only: DOM parsing can tell that a page
        # is schema-empty, but only the screenshot summary distinguishes settled empty
        # pages from loading shells.
        if merged.get("schema_empty_page") is True:
            empty_page_visible = visual_summary.get("empty_page_visible") is True
            loading_state_visible = visual_summary.get("loading_state_visible") is True
            if loading_state_visible:
                merged["empty_page_visual_state"] = "loading_or_progress"
            elif empty_page_visible:
                merged["observed_empty_page"] = True
                merged["empty_page_observation_source"] = VISION_EVIDENCE_SOURCE
                merged["empty_page_visual_state"] = "settled_empty"
            else:
                merged["empty_page_visual_state"] = "unknown"
    elif not merged.get("visual_evidence_summary"):
        merged["visual_evidence_summary"] = "Screenshot captured because DOM evidence indicated challenge state."

    merged["visual_evidence_omissions"] = list(dict.fromkeys(omissions))[:_MAX_VISUAL_OMISSIONS]
    return merged


def _parse_workflow_blocks(workflow_yaml: str | None) -> list[dict[str, Any]]:
    if not workflow_yaml:
        return []
    try:
        parsed = safe_load_no_dates(workflow_yaml)
    except yaml.YAMLError:
        return []
    if not isinstance(parsed, dict):
        return []
    definition = parsed.get("workflow_definition")
    if not isinstance(definition, dict):
        return []
    blocks = definition.get("blocks")
    return [block for block in blocks if isinstance(block, dict)] if isinstance(blocks, list) else []


def workflow_target_url(workflow_yaml: str | None) -> str | None:
    for block in _parse_workflow_blocks(workflow_yaml):
        url = block.get("url")
        if isinstance(url, str) and url.strip():
            return url.strip()
    return None


def _block_url(block: dict[str, Any]) -> str | None:
    url = block.get("url")
    return url.strip() if isinstance(url, str) and url.strip() else None


def _block_acts_on_page(block: dict[str, Any]) -> str | None:
    """How a block acts on a live page, or None if it is page-independent.

    "url"        - carries a target url (goto_url, navigation/login with a url, a
                   code block referencing a url); acts on that url's page.
    "interaction"- a no-url navigation/action/file_download/file_upload: an
                   interaction (click/fill/submit/download/upload) on the current page.
    "frontier"   - a no-url login/extraction/validation: reads or authenticates on
                   whatever page the workflow has reached.

    A pure code/transform block (no url, not an interaction/reading type) returns
    None and is not gated.
    """
    block_type = str(block.get("block_type") or "").strip().lower()
    if _block_url(block):
        return "url"
    if block_type in _INTERACTION_NO_URL_BLOCK_TYPES:
        return "interaction"
    if block_type in _FRONTIER_NO_URL_BLOCK_TYPES:
        return "frontier"
    return None


def _mode_requires_evidence(ctx: Any) -> bool:
    mode_value = getattr(getattr(getattr(ctx, "turn_intent", None), "mode", None), "value", None)
    phase = getattr(ctx, "build_phase", None)
    return (
        isinstance(mode_value, str)
        and mode_value in _BUILD_MODE_VALUES
        and phase
        in (
            BuildPhase.COMPOSING,
            BuildPhase.TESTING,
        )
    )


def _gated_page_acting_blocks(workflow_yaml: str | None, previous_workflow_yaml: str | None) -> list[dict[str, Any]]:
    """New-or-url-changed blocks that act on a page and so need observed evidence.

    Block-type-agnostic: any block carrying a url (goto_url past the entrypoint,
    navigation/login/code with a url) is gated, closing the goto_url/code escape;
    no-url navigation/login/extraction/validation blocks act on the current
    frontier. The first goto_url (the entrypoint scaffold) is exempt so the agent
    can record it and scout from it (SKY-10346). A url-bearing block whose url
    changed under the same label is re-gated so an edit cannot retarget a block to
    an unobserved page.

    target_url is the page the block acts on (own url, else nearest preceding
    goto_url, else the workflow entrypoint) for path/origin evidence matching.
    """
    previous_by_key = {
        (str(block.get("label") or ""), str(block.get("block_type") or "").strip().lower()): _block_url(block) or ""
        for block in _parse_workflow_blocks(previous_workflow_yaml)
    }
    gated: list[dict[str, Any]] = []
    nearest_goto: str | None = None
    fallback_url = workflow_target_url(workflow_yaml)
    no_url_interaction_since_url = False
    click_reached_observation_required = False
    for index, block in enumerate(_parse_workflow_blocks(workflow_yaml)):
        block_type = str(block.get("block_type") or "").strip().lower()
        url = _block_url(block)
        if url:
            nearest_goto = url
            no_url_interaction_since_url = False
            click_reached_observation_required = False
        if index == 0 and block_type == _GOTO_URL_BLOCK_TYPE:
            # entrypoint scaffold — ungated so the agent can record it and scout from it.
            continue
        acts_via = _block_acts_on_page(block)
        if acts_via is None:
            continue
        label = str(block.get("label") or "<missing label>")
        key = (label, block_type)
        is_new = key not in previous_by_key
        is_changed = (acts_via == "url") and (not is_new) and (previous_by_key.get(key) or "") != (url or "")
        if is_new or is_changed:
            gated.append(
                {
                    "label": label,
                    "block_type": block_type,
                    "acts_via": acts_via,
                    "target_url": url or nearest_goto or fallback_url,
                    "requires_observation_ref": acts_via != "url"
                    and (
                        click_reached_observation_required
                        or (block_type in _POST_INTERACTION_OBS_REQ_BLOCK_TYPES and no_url_interaction_since_url)
                    ),
                }
            )
        if acts_via == "interaction":
            no_url_interaction_since_url = True
            if block_type in _POST_INTERACTION_OBS_REQ_BLOCK_TYPES:
                click_reached_observation_required = True
    return gated


def _changed_goto_url_blocks(workflow_yaml: str | None, previous_workflow_yaml: str | None) -> list[dict[str, str]]:
    previous_urls = {
        (str(block.get("label") or ""), str(block.get("block_type") or "").strip().lower()): str(block.get("url") or "")
        for block in _parse_workflow_blocks(previous_workflow_yaml)
    }
    blocks: list[dict[str, str]] = []
    for block in _parse_workflow_blocks(workflow_yaml):
        block_type = str(block.get("block_type") or "").strip().lower()
        if block_type != _GOTO_URL_BLOCK_TYPE:
            continue
        label = str(block.get("label") or "<missing label>")
        url = str(block.get("url") or "").strip()
        if not url:
            continue
        prior_url = previous_urls.get((label, block_type))
        if prior_url == url:
            continue
        blocks.append({"label": label, "url": url})
    return blocks


def _format_page_block_findings(blocks: list[dict[str, str]]) -> str:
    return ", ".join(f"{block['label']} ({block['block_type']})" for block in blocks[:5])


def _same_page(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        left_parsed = urlparse(left)
        right_parsed = urlparse(right)
    except Exception:
        return False
    if not left_parsed.netloc or not right_parsed.netloc:
        return False
    if left_parsed.netloc.lower() != right_parsed.netloc.lower():
        return False
    left_path = (left_parsed.path or "/").rstrip("/") or "/"
    right_path = (right_parsed.path or "/").rstrip("/") or "/"
    return left_path == right_path


def _same_origin(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        left_parsed = urlparse(left)
        right_parsed = urlparse(right)
    except Exception:
        return False
    if not left_parsed.netloc or not right_parsed.netloc:
        return False
    return left_parsed.netloc.lower() == right_parsed.netloc.lower()


def _same_url_ignoring_fragment(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        left_parsed = urlparse(left)
        right_parsed = urlparse(right)
    except Exception:
        return False
    return left_parsed._replace(fragment="").geturl() == right_parsed._replace(fragment="").geturl()


def _post_run_recovery_state(ctx: _PostRunCompositionContext) -> bool:
    if any(ctx.per_tool_budget_problem_block_labels):
        return True
    if any(ctx.workflow_verification_evidence.per_tool_budget_on_block):
        return True
    if ctx.post_run_page_observation_after_failed_test is True:
        return True
    return ctx.last_failure_category_top == "PER_TOOL_BUDGET"


def _post_run_observed_url_goto_error(
    ctx: _PostRunCompositionContext,
    workflow_yaml: str | None,
    previous_workflow_yaml: str | None,
) -> str | None:
    if not previous_workflow_yaml or not _post_run_recovery_state(ctx):
        return None

    evidence = ctx.composition_page_evidence
    if not isinstance(evidence, dict) or evidence.get("observed_after_workflow_run") is not True:
        return None
    observed_url = evidence.get("current_url")
    if not isinstance(observed_url, str) or not observed_url.strip():
        return None

    offending = [
        block
        for block in _changed_goto_url_blocks(workflow_yaml, previous_workflow_yaml)
        if _same_url_ignoring_fragment(block.get("url"), observed_url)
    ]
    if not offending:
        return None

    labels = ", ".join(block["label"] for block in offending[:5])
    return (
        "Workflow validation failed: the draft is trying to persist a post-run browser URL as a new goto_url "
        "block after an incomplete or budgeted run. That URL may encode record-specific, result-page, or "
        "session state. Keep the reusable entrypoint and verified upstream blocks, then either extract from "
        "the observed current page, split or replace the budgeted frontier into smaller reusable UI actions, "
        "or report partial verification. "
        f"Offending goto_url block(s): {labels}."
    )


def has_bounded_page_schema(evidence: dict[str, Any]) -> bool:
    for key in ("forms", "navigation_targets", "result_containers", "challenge_controls"):
        value = evidence.get(key)
        if isinstance(value, list) and value:
            return True
    modal_overlays = evidence.get("modal_overlays")
    if isinstance(modal_overlays, list):
        for overlay in modal_overlays:
            if not isinstance(overlay, dict):
                continue
            dismiss_controls = overlay.get("dismiss_controls")
            if isinstance(dismiss_controls, list) and dismiss_controls:
                return True
    page_obstructions = evidence.get("page_obstructions")
    if isinstance(page_obstructions, list):
        for obstruction in page_obstructions:
            if not isinstance(obstruction, dict):
                continue
            visible_controls = obstruction.get("visible_controls")
            if isinstance(visible_controls, list) and visible_controls:
                return True
    challenge_state = evidence.get("challenge_state")
    if isinstance(challenge_state, dict) and challenge_state.get("detected") is True:
        return True
    # A visually confirmed settled-empty page is sufficient composition evidence
    # even though it has no DOM controls to act on.
    return evidence.get("observed_empty_page") is True


def has_actionable_steer_content(evidence: dict[str, Any]) -> bool:
    """Steer gate wider than has_bounded_page_schema: it also passes pages whose only
    affordances are standalone clickable controls, which stay steer-able yet must never
    feed has_bounded_page_schema or the no-progress reset.
    """
    if has_bounded_page_schema(evidence):
        return True
    clickable_controls = evidence.get("clickable_controls")
    return isinstance(clickable_controls, list) and bool(clickable_controls)


def _is_scout_interaction_evidence(evidence: dict[str, Any]) -> bool:
    # A scout interaction that resolved a concrete selector proves the page rendered
    # and the element was actionable, so it is non-hollow evidence of the reached
    # page even without a captured page schema.
    if evidence.get("source_tool") != SCOUT_INTERACTION_EVIDENCE_TOOL:
        return False
    selector = evidence.get("interaction_selector")
    return isinstance(selector, str) and bool(selector.strip())


def _evidence_matches_target(
    evidence: dict[str, Any] | None,
    target_url: str | None,
    *,
    allow_post_run_browser_observation: bool = False,
) -> bool:
    if not evidence or not target_url:
        return False
    source_tool = evidence.get("source_tool")
    current_url = evidence.get("current_url")
    inspected_url = evidence.get("inspected_url")
    current_url = current_url if isinstance(current_url, str) else None
    inspected_url = inspected_url if isinstance(inspected_url, str) else None
    if _is_scout_interaction_evidence(evidence):
        if _same_page(current_url, target_url) or _same_page(inspected_url, target_url):
            return True
        if allow_post_run_browser_observation and (
            _same_origin(current_url, target_url) or _same_origin(inspected_url, target_url)
        ):
            return True
    # A hollow inspection (empty forms/links/result containers, no detected
    # challenge) is not observation — `inspect_page_for_composition` can return an
    # empty schema when the page had not rendered at capture time, so URL match
    # alone must not satisfy the gate. Require a bounded page schema for every
    # evidence source, the inspector included.
    if source_tool == _SCHEMA_EVIDENCE_TOOL and has_bounded_page_schema(evidence):
        if _same_page(current_url, target_url) or _same_page(inspected_url, target_url):
            return True
    if source_tool in _STRUCTURED_BROWSER_EVIDENCE_TOOLS and has_bounded_page_schema(evidence):
        if _same_page(current_url, target_url) or _same_page(inspected_url, target_url):
            return True
        if allow_post_run_browser_observation and (
            _same_origin(current_url, target_url) or _same_origin(inspected_url, target_url)
        ):
            return True
    if (
        allow_post_run_browser_observation
        and source_tool in _POST_RUN_CONTINUATION_EVIDENCE_TOOLS
        and has_bounded_page_schema(evidence)
        and evidence.get("observed_after_workflow_run") is True
    ):
        return _same_origin(current_url, target_url) or _same_origin(inspected_url, target_url)
    return False


def _turn_evidence_sources(ctx: Any) -> list[dict[str, Any]]:
    """Every page-evidence packet available this turn: the flow-evidence trajectory
    (one packet per scouted page) plus the legacy single composition_page_evidence
    slot (back-compat for callers and tests that set only it).
    """
    sources: list[dict[str, Any]] = []
    for entry in getattr(ctx, "flow_evidence", None) or []:
        if isinstance(entry, dict):
            packet = entry.get("evidence")
            if isinstance(packet, dict):
                sources.append(packet)
    single = getattr(ctx, "composition_page_evidence", None)
    if isinstance(single, dict):
        sources.append(single)
    return sources


def turn_has_scout_interaction(ctx: Any) -> bool:
    """True when this turn's evidence trajectory carries a scout_interaction observation that
    resolved a concrete selector — proof the model scout-ACTED an affordance (clicked it) rather
    than only inspecting the page passively."""
    return any(_is_scout_interaction_evidence(evidence) for evidence in _turn_evidence_sources(ctx))


def _prior_observed_pages(ctx: Any) -> list[dict[str, Any]]:
    return [page for page in (getattr(ctx, "prior_observed_acted_pages", None) or []) if isinstance(page, dict)]


def _flow_evidence_by_step(ctx: Any) -> dict[int, tuple[dict[str, Any], str]]:
    by_step: dict[int, tuple[dict[str, Any], str]] = {}
    for entry in getattr(ctx, "flow_evidence", None) or []:
        if not isinstance(entry, dict):
            continue
        packet = entry.get("evidence")
        if not isinstance(packet, dict):
            continue
        step = entry.get("step")
        if isinstance(step, bool) or not isinstance(step, int):
            continue
        if step in by_step:
            # Duplicates here mean malformed persisted or deserialized evidence.
            retained_packet, retained_reached_via = by_step[step]
            LOG.warning(
                "copilot_flow_evidence_duplicate_step_ignored",
                observation_step=step,
                retained_reached_via=retained_reached_via,
                ignored_reached_via=str(entry.get("reached_via") or ""),
                retained_url=retained_packet.get("current_url") or retained_packet.get("inspected_url"),
                ignored_url=packet.get("current_url") or packet.get("inspected_url"),
                prior_retained_steps_count=len(by_step),
            )
            continue
        by_step[step] = (packet, str(entry.get("reached_via") or ""))
    return by_step


def _iter_block_observation_ref_items(value: Any, *, warn_malformed: bool) -> Iterable[tuple[Any, Any]] | None:
    if isinstance(value, dict):
        return value.items()
    if isinstance(value, list):
        items: list[tuple[Any, Any]] = []
        for item in value:
            if isinstance(item, dict):
                items.append((item.get("label"), item.get("observation_step")))
            elif hasattr(item, "label") and hasattr(item, "observation_step"):
                # Accept the typed ref shape without coupling the composition gate
                # to a concrete pydantic model.
                items.append((item.label, item.observation_step))
            elif warn_malformed:
                LOG.warning(
                    "copilot_block_observation_ref_malformed_item_ignored",
                    item_type=type(item).__name__,
                )
        return items
    return None


def normalize_block_observation_refs(value: Any) -> dict[str, int]:
    items = _iter_block_observation_ref_items(value, warn_malformed=True)
    if items is None:
        LOG.warning(
            "copilot_block_observation_refs_unexpected_type_ignored",
            value_type=type(value).__name__,
        )
        return {}
    refs: dict[str, int] = {}
    for label, step in items:
        if not isinstance(label, str) or not label.strip():
            continue
        if isinstance(step, bool):
            continue
        if isinstance(step, int):
            refs[label.strip()] = step
        elif isinstance(step, str):
            # String steps are ignored so callers can repair malformed refs.
            LOG.warning(
                "copilot_block_observation_ref_string_step_ignored",
                label=label.strip(),
                step_length=len(step),
            )
    return refs


def _block_observation_refs(ctx: Any) -> dict[str, int]:
    return normalize_block_observation_refs(getattr(ctx, "block_observation_refs", None))


def _evidence_observed_url(evidence: dict[str, Any]) -> str | None:
    for key in ("current_url", "inspected_url"):
        value = evidence.get(key)
        # "current_page" is the sentinel for inspecting the current browser page
        # without a known target URL.
        if isinstance(value, str) and value.strip() and value != "current_page":
            return value.strip()
    return None


def _page_observed(ctx: Any, target_url: str | None, *, allow_post_run: bool) -> bool:
    if not target_url:
        return False
    for evidence in _turn_evidence_sources(ctx):
        if _evidence_matches_target(evidence, target_url, allow_post_run_browser_observation=allow_post_run):
            return True
    # Cross-turn credit requires the SAME page (netloc+path), not just same
    # origin: the compact summary only proves which page was observed, so a
    # same-origin relaxation would credit a gated block on a sibling page the
    # agent never saw. The within-turn post-run same-origin continuation still
    # applies above via _evidence_matches_target.
    for page in _prior_observed_pages(ctx):
        if page.get("had_bounded_schema") and _same_page(page.get("url"), target_url):
            return True
    return False


def _associated_observation_satisfies_block(
    evidence: dict[str, Any],
    target_url: str | None,
    *,
    acts_via: str,
    reached_via: str,
    requires_observation_ref: bool,
    allow_post_run: bool,
) -> bool:
    if acts_via == "url":
        # URL blocks are grounded by target_url, not observation-ref gates.
        if requires_observation_ref:
            return False
        return _evidence_matches_target(evidence, target_url, allow_post_run_browser_observation=allow_post_run)
    if requires_observation_ref and reached_via not in {"interaction", "post_run"}:
        return False

    observed_url = _evidence_observed_url(evidence)
    if not observed_url:
        return False
    return _evidence_matches_target(evidence, observed_url, allow_post_run_browser_observation=allow_post_run)


def _current_page_evidence_has_reached_page_credit(
    evidence: dict[str, Any],
    reached_via: str,
    *,
    flow_evidence_by_step: dict[int, tuple[dict[str, Any], str]],
) -> bool:
    if reached_via != "current_page" or not has_bounded_page_schema(evidence):
        return False
    observed_url = _evidence_observed_url(evidence)
    if not observed_url:
        return False
    for prior_evidence, prior_reached_via in flow_evidence_by_step.values():
        if prior_reached_via not in {"interaction", "post_run"}:
            continue
        if not has_bounded_page_schema(prior_evidence):
            continue
        if _same_page(observed_url, _evidence_observed_url(prior_evidence)):
            return True
    return False


def _auto_credit_interaction_observation(
    flow_evidence_by_step: dict[int, tuple[dict[str, Any], str]],
    consumed_steps: set[int],
) -> bool:
    # Bind by trajectory recency, never by source_url: a SPA holds one URL across
    # interactions, so URL identity would mis-bind. Consume-once keeps each block on a
    # distinct interaction.
    for step in sorted(flow_evidence_by_step, reverse=True):
        if step in consumed_steps:
            continue
        evidence, reached_via = flow_evidence_by_step[step]
        if reached_via != "interaction":
            continue
        if not (_is_scout_interaction_evidence(evidence) or has_bounded_page_schema(evidence)):
            continue
        consumed_steps.add(step)
        LOG.info(
            "copilot_gate_auto_credited_interaction",
            observation_step=step,
            source_tool=evidence.get("source_tool"),
            interaction_selector=evidence.get("interaction_selector"),
        )
        return True
    return False


def _block_has_observed_page(
    ctx: Any,
    block: dict[str, Any],
    *,
    allow_post_run: bool,
    flow_evidence_by_step: dict[int, tuple[dict[str, Any], str]],
    block_observation_refs: dict[str, int],
    consumed_steps: set[int],
) -> bool:
    label = str(block.get("label") or "")
    if label and label in block_observation_refs:
        step = block_observation_refs[label]
        evidence_entry = flow_evidence_by_step.get(step)
        if evidence_entry is not None:
            evidence, reached_via = evidence_entry
            effective_reached_via = (
                "interaction"
                if _current_page_evidence_has_reached_page_credit(
                    evidence,
                    reached_via,
                    flow_evidence_by_step=flow_evidence_by_step,
                )
                else reached_via
            )
            if _associated_observation_satisfies_block(
                evidence,
                block.get("target_url"),
                acts_via=str(block.get("acts_via") or ""),
                reached_via=effective_reached_via,
                requires_observation_ref=block.get("requires_observation_ref") is True,
                allow_post_run=allow_post_run,
            ):
                if effective_reached_via == "interaction":
                    consumed_steps.add(step)
                return True

    if block.get("requires_observation_ref") is True:
        return _auto_credit_interaction_observation(flow_evidence_by_step, consumed_steps)
    return _page_observed(ctx, block.get("target_url"), allow_post_run=allow_post_run)


def _missing_observation_ref_step(
    block: dict[str, Any],
    *,
    flow_evidence_by_step: dict[int, tuple[dict[str, Any], str]],
    block_observation_refs: dict[str, int],
) -> int | None:
    label = str(block.get("label") or "")
    if not label or label not in block_observation_refs:
        return None
    step = block_observation_refs[label]
    return None if step in flow_evidence_by_step else step


def _raw_block_observation_ref_step(value: Any, label: str) -> object | None:
    if not label:
        return None
    items = _iter_block_observation_ref_items(value, warn_malformed=False)
    if items is None:
        return None
    for item_label, item_step in items:
        if isinstance(item_label, str) and item_label.strip() == label:
            return item_step
    return None


def _string_observation_ref_step(block: dict[str, Any], raw_block_observation_refs: Any) -> str | None:
    label = str(block.get("label") or "")
    raw_step = _raw_block_observation_ref_step(raw_block_observation_refs, label)
    return raw_step if isinstance(raw_step, str) else None


def _required_observation_ref_missing(block: dict[str, Any], block_observation_refs: dict[str, int]) -> bool:
    if block.get("requires_observation_ref") is not True:
        return False
    label = str(block.get("label") or "")
    return bool(label and label not in block_observation_refs)


def _wrong_reached_via_observation_ref(
    block: dict[str, Any],
    *,
    flow_evidence_by_step: dict[int, tuple[dict[str, Any], str]],
    block_observation_refs: dict[str, int],
) -> tuple[int, str] | None:
    if block.get("requires_observation_ref") is not True:
        return None
    label = str(block.get("label") or "")
    if not label or label not in block_observation_refs:
        return None
    step = block_observation_refs[label]
    evidence_entry = flow_evidence_by_step.get(step)
    if evidence_entry is None:
        return None
    evidence, reached_via = evidence_entry
    if reached_via in {"interaction", "post_run"}:
        return None
    if _current_page_evidence_has_reached_page_credit(
        evidence,
        reached_via,
        flow_evidence_by_step=flow_evidence_by_step,
    ):
        return None
    return step, reached_via or "<missing>"


def composition_page_evidence_error(
    ctx: Any,
    workflow_yaml: str | None,
    *,
    block_observation_refs: dict[str, int] | None = None,
    raw_block_observation_refs: Any | None = None,
) -> str | None:
    """Return a mutation error when a build adds page-acting blocks before observation.

    Deliberately structural rather than semantic: every block that acts on a page
    — block-type-agnostic, including goto_url/code blocks that carry a url, and
    each page across a multi-page flow — needs observed evidence of that page
    first. Whether the agent observed the *right* live state (e.g. a control that
    only appears after a click) is driven by the agent's live scouting and measured
    by evals, not enforced by a classifier in the mutation path.
    """

    if not _mode_requires_evidence(ctx):
        return None
    previous_workflow_yaml = getattr(ctx, "workflow_yaml", None)
    post_run_url_error = _post_run_observed_url_goto_error(ctx, workflow_yaml, previous_workflow_yaml)
    if post_run_url_error:
        return post_run_url_error

    gated_blocks = _gated_page_acting_blocks(workflow_yaml, previous_workflow_yaml)
    if not gated_blocks:
        return None

    allow_post_run = bool(previous_workflow_yaml)
    flow_evidence_by_step = _flow_evidence_by_step(ctx)
    if raw_block_observation_refs is None:
        raw_block_observation_refs = getattr(
            ctx,
            "raw_block_observation_refs",
            getattr(ctx, "block_observation_refs", None),
        )
    if block_observation_refs is None:
        block_observation_refs = _block_observation_refs(ctx)
    consumed_steps: set[int] = set()
    for block in gated_blocks:
        target_url = block["target_url"]
        if not _block_has_observed_page(
            ctx,
            block,
            allow_post_run=allow_post_run,
            flow_evidence_by_step=flow_evidence_by_step,
            block_observation_refs=block_observation_refs,
            consumed_steps=consumed_steps,
        ):
            missing_step = _missing_observation_ref_step(
                block,
                flow_evidence_by_step=flow_evidence_by_step,
                block_observation_refs=block_observation_refs,
            )
            string_step = _string_observation_ref_step(block, raw_block_observation_refs)
            wrong_reached_via = _wrong_reached_via_observation_ref(
                block,
                flow_evidence_by_step=flow_evidence_by_step,
                block_observation_refs=block_observation_refs,
            )
            if string_step is not None:
                return (
                    "Workflow validation failed: a block_observation_refs entry uses observation_step "
                    f"{string_step!r} as a string. Pass the integer observation_step returned by "
                    "inspect_page_for_composition or evaluate for click-reached blocks. "
                    f"Offending blocks: {_format_page_block_findings([block])}"
                )
            if _required_observation_ref_missing(block, block_observation_refs):
                return (
                    "Workflow validation failed: a click-reached block requires a block_observation_refs entry. "
                    "Pass an interaction- or post_run-reached observation_step for click-reached blocks before "
                    "composing them. "
                    f"Offending blocks: {_format_page_block_findings([block])}"
                )
            if wrong_reached_via is not None:
                step, reached_via = wrong_reached_via
                return (
                    "Workflow validation failed: a block references observation_step "
                    f"{step}, but that observed page was reached via {reached_via!r}. "
                    "Pass an interaction- or post_run-reached observation_step for click-reached blocks. "
                    f"Offending blocks: {_format_page_block_findings([block])}"
                )
            if missing_step is not None:
                min_available_step = min(flow_evidence_by_step) if flow_evidence_by_step else None
                missing_reason = (
                    "that observed page evidence is no longer available in the flow-evidence window"
                    if min_available_step is not None and missing_step < min_available_step
                    else "that observation step was not found in flow evidence"
                )
                return (
                    "Workflow validation failed: a block references observation_step "
                    f"{missing_step}, but {missing_reason}. "
                    "Inspect or evaluate the reached page again and pass the new observation_step in "
                    "block_observation_refs before composing page-dependent blocks. "
                    f"Offending blocks: {_format_page_block_findings([block])}"
                )
            return (
                "Workflow validation failed: page-dependent build blocks need observed page evidence before they are "
                f"authored. Call inspect_page_for_composition(target_url={target_url!r}) before composing page-dependent "
                "blocks, or save only the initial goto_url block and inspect the reached page before the next mutation. "
                f"Offending blocks: {_format_page_block_findings([block])}"
            )

    # Matched page evidence may ground a multi-block mutation. This gate enforces
    # observation before page-dependent composition; it does not prescribe a
    # one-block-per-observation workflow construction style.

    return None


def _empty_evidence(inspected_url: str, current_url: str) -> dict[str, Any]:
    return {
        "inspected_url": inspected_url,
        "current_url": current_url,
        "page_title": "",
        "forms": [],
        "navigation_targets": [],
        "result_containers": [],
        "result_containers_truncated": False,
        "key_value_relations": [],
        "key_value_relations_truncated": False,
        **_clickable_controls_channel([]),
        "visible_text_excerpt": "",
        "anti_bot_indicators": [],
        "challenge_controls": [],
        "modal_overlays": [],
        "page_obstructions": [],
        "visual_obstruction_candidates": [],
        "schema_empty_page": False,
        "observed_empty_page": False,
        "empty_page_visual_state": None,
        "evidence_confidence": 0.0,
        "source_tool": "inspect_page_for_composition",
        **_evidence_metadata([]),
    }


def _node_text(node: Any) -> str:
    try:
        return node.get_text(" ", strip=True)
    except Exception:
        return ""


def _schema_text(value: str, max_chars: int) -> str:
    return " ".join((value or "").split())[:max_chars]


def _attr_value(node: Any, key: str) -> str:
    value = node.get(key) if hasattr(node, "get") else None
    return value.strip() if isinstance(value, str) else ""


def _classes_for(node: Any) -> list[str]:
    class_value = node.get("class") if hasattr(node, "get") else None
    if isinstance(class_value, list):
        return [str(item).strip() for item in class_value if str(item).strip()]
    if isinstance(class_value, str):
        return [part for part in class_value.split() if part]
    return []


def _inline_style_properties(node: Any) -> dict[str, str]:
    style = _attr_value(node, "style")
    if not style:
        return {}
    properties: dict[str, str] = {}
    for declaration in style.split(";"):
        if ":" not in declaration:
            continue
        key, value = declaration.split(":", 1)
        key = key.strip().lower()
        value = value.strip().lower()
        if key and value:
            properties[key] = value
    return properties


def _css_zero(value: str) -> bool:
    return value.replace(" ", "") in {"0", "0px", "0%", "0rem", "0em", "0vh", "0vw"}


def _css_full_width(value: str) -> bool:
    compact = value.replace(" ", "")
    return compact in {"100%", "100vw", "100dvw", "100lvw", "100svw"}


def _css_full_height(value: str) -> bool:
    compact = value.replace(" ", "")
    return compact in {"100%", "100vh", "100dvh", "100lvh", "100svh"}


def _z_index_is_high(value: str) -> bool:
    try:
        return int(float(value)) >= 10
    except (TypeError, ValueError):
        return False


def _style_covers_viewport(properties: dict[str, str]) -> bool:
    inset = properties.get("inset")
    if inset is not None and all(_css_zero(part) for part in inset.split()):
        return True
    covers_edges = all(_css_zero(properties.get(edge, "")) for edge in ("top", "right", "bottom", "left"))
    if covers_edges:
        return True
    starts_at_origin = _css_zero(properties.get("top", "")) and _css_zero(properties.get("left", ""))
    return (
        starts_at_origin
        and _css_full_width(properties.get("width", ""))
        and _css_full_height(properties.get("height", "") or properties.get("min-height", ""))
    )


def _node_has_clickable_descendant(node: Any) -> bool:
    if not hasattr(node, "find_all"):
        return False
    for control in node.find_all(["button", "a", "input"]):
        tag_name = str(getattr(control, "name", "") or "").lower()
        if tag_name == "input":
            field_type = str(control.get("type") or "").lower()
            if field_type not in {"button", "submit", "reset"}:
                continue
        if _node_text(control) or _attr_value(control, "value") or _attr_value(control, "aria-label"):
            return True
    for control in node.find_all(attrs={"role": "button"}):
        if _node_text(control) or _attr_value(control, "aria-label"):
            return True
    return False


def _visual_obstruction_candidates(soup: Any) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for node in soup.find_all(True):
        if len(candidates) >= _MAX_PAGE_OBSTRUCTIONS:
            break
        properties = _inline_style_properties(node)
        position = properties.get("position", "")
        if position not in {"fixed", "sticky"}:
            continue
        if not _z_index_is_high(properties.get("z-index", "")):
            continue
        if not _style_covers_viewport(properties):
            continue
        candidates.append(
            {
                "source": DOM_STYLE_EVIDENCE_SOURCE,
                "position": position,
                "coverage": "viewport",
                "has_visible_controls": _node_has_clickable_descendant(node),
            }
        )
    return candidates


def _css_attr(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _simple_css_identifier(value: str) -> bool:
    if not value:
        return False
    first = value[0]
    if not (first.isalpha() or first in {"_", "-"}):
        return False
    return all(char.isalnum() or char in {"_", "-"} for char in value[1:])


def _class_selector(classes: list[str]) -> str:
    parts: list[str] = []
    for class_name in classes[:3]:
        if _simple_css_identifier(class_name):
            parts.append(f".{class_name}")
        else:
            parts.append(f'[class~="{_css_attr(class_name)}"]')
    return "".join(parts)


def _selector_for(node: Any) -> str:
    tag_name = getattr(node, "name", None) or "*"
    node_id = _attr_value(node, "id")
    if node_id:
        return f"#{node_id}"
    node_name = _attr_value(node, "name")
    node_value = _attr_value(node, "value")
    if node_name and node_value:
        return f'{tag_name}[name="{_css_attr(node_name)}"][value="{_css_attr(node_value)}"]'
    classes = _classes_for(node)
    class_selector = _class_selector(classes)
    if class_selector and node_value:
        return f'{tag_name}{class_selector}[value="{_css_attr(node_value)}"]'
    if node_name:
        return f'{tag_name}[name="{_css_attr(node_name)}"]'
    href = _attr_value(node, "href")
    if tag_name == "a" and href:
        return f'a[href="{_css_attr(href)}"]'
    if class_selector:
        return f"{tag_name}{class_selector}"
    return str(tag_name)


def _clickable_controls_channel(controls: list[dict[str, Any]] | None) -> dict[str, Any]:
    if not settings.COPILOT_CLICK_REPERCEPTION_ATTACH_ENABLED:
        return {}
    return {"clickable_controls": controls or []}


def _clickable_control_selector(node: Any) -> str:
    """Build a selector for a standalone clickable control, preferring attributes _selector_for
    never emits (data-action/aria-label) so a tile authored against [data-action=...] can be grounded."""
    tag_name = getattr(node, "name", None) or "*"
    node_id = _attr_value(node, "id")
    if node_id:
        return f"#{node_id}"
    data_action = _attr_value(node, "data-action")
    if data_action:
        return f'{tag_name}[data-action="{_css_attr(data_action)}"]'
    aria_label = _attr_value(node, "aria-label")
    if aria_label:
        return f'{tag_name}[aria-label="{_css_attr(aria_label)}"]'
    node_name = _attr_value(node, "name")
    node_value = _attr_value(node, "value")
    if node_name and node_value:
        return f'{tag_name}[name="{_css_attr(node_name)}"][value="{_css_attr(node_value)}"]'
    class_selector = _class_selector(_classes_for(node))
    if class_selector:
        return f"{tag_name}{class_selector}"
    return ""


def _clickable_control_text(node: Any) -> str:
    for value in (
        _node_text(node),
        _attr_value(node, "aria-label"),
        _attr_value(node, "value"),
        _attr_value(node, "title"),
    ):
        if value:
            return value
    return ""


def _selector_is_live_unique_in_soup(soup: Any, selector: str) -> bool:
    if not selector:
        return False
    try:
        return len(soup.select(selector)) == 1
    except Exception:
        return False


def _clickable_controls_html(soup: Any, *, used_selectors: set[str]) -> list[dict[str, Any]]:
    controls: list[dict[str, Any]] = []
    seen_selectors = set(used_selectors)
    seen_text: set[str] = set()
    try:
        candidates = soup.select('button, [role="button"], [data-action]')
    except Exception:
        candidates = soup.find_all("button")
    for node in candidates:
        if len(controls) >= _MAX_CLICKABLE_CONTROLS:
            break
        tag_name = str(node.name or "").lower()
        if tag_name in {"script", "style", "noscript"}:
            continue
        if hasattr(node, "find_parent") and node.find_parent("form") is not None:
            continue
        text = _schema_text(_clickable_control_text(node), 120)
        selector = _clickable_control_selector(node)
        if selector and selector not in seen_selectors and _selector_is_live_unique_in_soup(soup, selector):
            controls.append({"text": text, "selector": selector[:160], "tag": tag_name})
            seen_selectors.add(selector)
            if text:
                seen_text.add(text)
            continue
        if not text or text in seen_text:
            continue
        controls.append({"text": text, "tag": tag_name})
        seen_text.add(text)
    return controls


def _adjacent_text(field: Any) -> str:
    for siblings in (getattr(field, "next_siblings", []), getattr(field, "previous_siblings", [])):
        for index, sibling in enumerate(siblings):
            if index >= 4:
                break
            sibling_name = str(getattr(sibling, "name", "") or "").lower()
            if sibling_name in {"input", "select", "textarea", "button"}:
                # Stop at the next control so labels are not borrowed from a neighboring field.
                break
            text = _node_text(sibling) if sibling_name else str(sibling).strip()
            if text:
                return text
    return ""


def _parent_text_label(field: Any) -> str:
    for parent_name in ("td", "th", "li", "div", "span"):
        parent = field.find_parent(parent_name) if hasattr(field, "find_parent") else None
        if parent is None:
            continue
        text = _node_text(parent)
        if 0 < len(text) <= 240:
            return text
    return ""


def _select_options(node: Any) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for option in node.find_all("option")[:_MAX_SELECT_OPTIONS]:
        options.append(
            {
                "text": _node_text(option)[:120],
                "value": _attr_value(option, "value")[:160],
                "selected": bool(option.has_attr("selected")),
            }
        )
    return options


def _field_label(soup: Any, field: Any) -> str:
    field_id = _attr_value(field, "id")
    if field_id:
        label_tag = soup.find("label", attrs={"for": field_id})
        if label_tag is not None:
            label = _node_text(label_tag)
            if label:
                return label
    parent_label = field.find_parent("label") if hasattr(field, "find_parent") else None
    if parent_label is not None:
        label = _node_text(parent_label).replace(_node_text(field), "").strip()
        if label:
            return label
    for value in (
        _attr_value(field, "aria-label"),
        _adjacent_text(field),
        _parent_text_label(field),
        _attr_value(field, "title"),
        _attr_value(field, "value"),
    ):
        if value:
            return value
    return ""


def _page_title(soup: Any) -> str:
    parts: list[str] = []
    for tag_name in ("title", "h1"):
        tag = soup.find(tag_name)
        text = _node_text(tag) if tag is not None else ""
        if text and text not in parts:
            parts.append(text)
    return " ".join(parts)[:240]


def _result_row_text_is_content(text: str) -> bool:
    normalized = " ".join(text.lower().split())
    return bool(normalized) and not any(pattern in normalized for pattern in _EMPTY_RESULT_TEXT_PATTERNS)


def _selector_match_count(soup: Any, selector: str) -> int:
    try:
        return len(soup.select(selector))
    except Exception:
        return 0


def _key_value_relations(soup: Any) -> tuple[list[dict[str, Any]], bool]:
    relations: list[dict[str, Any]] = []
    truncated = False
    for node in soup.find_all(True):
        tag_name = str(getattr(node, "name", "") or "").lower()
        if tag_name in {"body", "form", "html", "table", "tbody", "thead", "tr"}:
            continue
        children = [child for child in node.find_all(recursive=False) if child.name]
        if len(children) != 2:
            continue
        if children[0].find(True) is not None:
            continue
        key_text = _schema_text(_node_text(children[0]), 120)
        value_text = _schema_text(_node_text(children[1]), 240)
        if not key_text or not value_text or key_text == value_text:
            continue
        if len(relations) >= _MAX_KEY_VALUE_RELATIONS:
            truncated = True
            break
        selector = _selector_for(node)[:160]
        match_count = _selector_match_count(soup, selector)
        if match_count <= 0:
            continue
        matches = soup.select(selector)
        try:
            position = matches.index(node)
        except ValueError:
            continue
        relations.append(
            {
                "key_text": key_text,
                "container_selector": selector,
                "container_match_count": match_count,
                "container_position": position,
                "value_child_index": 1,
                "direct_child_count": len(children),
                "visible": True,
                "value_visible": True,
            }
        )
    return relations, truncated


def _result_container_entry(node: Any, *, soup: Any) -> dict[str, Any]:
    tag_name = str(node.name or "").lower()
    node_id = str(node.get("id") or "")
    selector = _selector_for(node)[:160]
    entry: dict[str, Any] = {
        "tag": tag_name,
        "id": node_id[:120],
        "selector": selector,
        "selector_match_count": _selector_match_count(soup, selector),
        "visible": True,
    }
    if tag_name == "table":
        entry["row_selector"] = f"{selector} tbody tr"
        entry["expand_toggle_candidates"] = [
            f"{selector} tbody tr [aria-expanded]",
            f'{selector} tbody tr [role="button"]',
            f"{selector} tbody tr button",
            f"{selector} tbody tr a",
            f"{selector} tbody tr td:first-child",
        ]
        data_rows = [row for row in node.select("tbody tr") if row.find("td") is not None]
        if not data_rows:
            data_rows = [row for row in node.select("tr") if row.find("td") is not None]
        headers = [
            {"text": _schema_text(_node_text(header), 120), "column_index": index}
            for index, header in enumerate(node.select("thead th")[:_MAX_TABLE_HEADERS])
            if _schema_text(_node_text(header), 120)
        ]
        if headers:
            entry["headers"] = headers
        entry["row_count"] = len(data_rows)
        entry["rows_truncated"] = len(data_rows) > _MAX_RESULT_SAMPLE_ROWS
        entry["span_free"] = node.select_one("th[colspan], th[rowspan], td[colspan], td[rowspan]") is None
        entry["nested_table_free"] = node.find("table") is None
        entry["rows"] = [
            {
                "row_index": row_index,
                "visible": True,
                "has_row_header": row.select_one(":scope > th") is not None,
                "cells": [
                    {"column_index": column_index, "visible": True}
                    for column_index, _cell in enumerate(row.select(":scope > td")[:_MAX_TABLE_HEADERS])
                ],
            }
            for row_index, row in enumerate(data_rows[:_MAX_RESULT_SAMPLE_ROWS])
        ]
        sample_rows = [_schema_text(_node_text(row), 240) for row in data_rows]
        sample_rows = [row for row in sample_rows if _result_row_text_is_content(row)][:5]
        if sample_rows:
            entry["sample_rows"] = sample_rows
    else:
        text_excerpt = _schema_text(_node_text(node), 240)
        if text_excerpt:
            entry["text_excerpt"] = text_excerpt
    return entry


def _challenge_control_entry(node: Any) -> dict[str, Any]:
    tag_name = str(getattr(node, "name", "") or "").lower()
    entry: dict[str, Any] = {
        "tag": tag_name,
        "id": _attr_value(node, "id")[:120],
        "name": _attr_value(node, "name")[:120],
        "class": " ".join(_classes_for(node)[:5])[:160],
        "type": _attr_value(node, "type")[:40],
        "selector": _selector_for(node)[:160],
        "text": _schema_text(_node_text(node) or _attr_value(node, "aria-label"), 200),
    }
    for key in ("src", "title", "data-sitekey", "data-callback", "data-expired-callback", "data-error-callback"):
        value = _attr_value(node, key)
        if value:
            entry[key.replace("-", "_")] = value[:300]
    return {key: value for key, value in entry.items() if value}


def _challenge_controls(soup: Any) -> list[dict[str, Any]]:
    controls: list[dict[str, Any]] = []
    seen_selectors: set[str] = set()
    for node in soup.find_all(True):
        if len(controls) >= _MAX_CHALLENGE_CONTROLS:
            break
        identity = " ".join(
            str(value or "")
            for value in (
                getattr(node, "name", ""),
                _attr_value(node, "id"),
                _attr_value(node, "name"),
                _attr_value(node, "class"),
                _attr_value(node, "src"),
                _attr_value(node, "type"),
                _attr_value(node, "data-sitekey"),
                _attr_value(node, "data-callback"),
                _attr_value(node, "data-expired-callback"),
                _attr_value(node, "data-error-callback"),
                _attr_value(node, "aria-label"),
                _attr_value(node, "title"),
            )
        ).lower()
        if not any(pattern in identity for pattern in _ANTI_BOT_PATTERNS):
            continue
        # A widget inside a hidden ancestor (solved/stale challenge markup) may
        # trigger the visual fallback but must not read as a rendered control.
        if _is_hidden_modal_candidate(node):
            continue
        selector = _selector_for(node)[:160]
        if selector in seen_selectors:
            continue
        seen_selectors.add(selector)
        controls.append(_challenge_control_entry(node))
    return controls


def _modal_identity(node: Any) -> str:
    values = (
        getattr(node, "name", ""),
        _attr_value(node, "id"),
        " ".join(_classes_for(node)),
        _attr_value(node, "role"),
        _attr_value(node, "aria-label"),
        _attr_value(node, "title"),
        _attr_value(node, "data-testid"),
        _attr_value(node, "data-test"),
        _attr_value(node, "data-dismiss"),
    )
    return " ".join(str(value or "") for value in values).lower()


def _is_modal_overlay_candidate(node: Any) -> bool:
    role = _attr_value(node, "role").strip().lower()
    if role in _MODAL_ROLE_VALUES:
        return True
    if _attr_value(node, "aria-modal").strip().lower() == "true":
        return True
    return any(pattern in _modal_identity(node) for pattern in _MODAL_IDENTITY_PATTERNS)


def _is_hidden_modal_candidate(node: Any) -> bool:
    current = node
    while current is not None:
        if _attr_value(current, "aria-hidden").strip().lower() == "true":
            return True
        if hasattr(current, "has_attr") and current.has_attr("hidden"):
            return True
        style = _attr_value(current, "style").replace(" ", "").lower()
        if "display:none" in style or "visibility:hidden" in style:
            return True
        current = getattr(current, "parent", None)
    return False


def _is_css_hidden_node(node: Any) -> bool:
    if not hasattr(node, "has_attr"):
        return False
    if node.has_attr("hidden"):
        return True
    style = _attr_value(node, "style").replace(" ", "").lower()
    return "display:none" in style or "visibility:hidden" in style


def _modal_dismiss_controls(node: Any) -> list[dict[str, Any]]:
    controls: list[dict[str, Any]] = []
    seen_selectors: set[str] = set()
    for control in node.find_all(["button", "a", "input"]):
        if len(controls) >= _MAX_MODAL_DISMISS_CONTROLS:
            break
        selector = _selector_for(control)[:160]
        if selector in seen_selectors:
            continue
        text = _schema_text(_node_text(control) or _attr_value(control, "value"), 120)
        aria_label = _schema_text(_attr_value(control, "aria-label"), 120)
        title = _schema_text(_attr_value(control, "title"), 120)
        explicit_values = {text.strip().lower(), aria_label.strip().lower(), title.strip().lower()}
        identity = f"{text} {aria_label} {title} {_modal_identity(control)}".lower()
        # Controls are typed as Any because BeautifulSoup is optional at import time.
        has_data_dismiss = hasattr(control, "has_attr") and control.has_attr("data-dismiss")
        has_symbol_hint = bool(explicit_values & _MODAL_DISMISS_SYMBOLS)
        has_text_hint = any(hint in identity for hint in _MODAL_DISMISS_HINTS)
        if not (has_data_dismiss or has_symbol_hint or has_text_hint):
            continue
        seen_selectors.add(selector)
        controls.append(
            {
                "tag": str(getattr(control, "name", "") or "").lower()[:40],
                "text": text,
                "aria_label": aria_label,
                "title": title,
                "selector": selector,
                "type": _attr_value(control, "type")[:40],
            }
        )
    return [{key: value for key, value in entry.items() if value} for entry in controls]


def _modal_overlay_entry(node: Any) -> dict[str, Any]:
    return {
        "role": _attr_value(node, "role")[:80],
        "aria_modal": _attr_value(node, "aria-modal").strip().lower() == "true",
        "id": _attr_value(node, "id")[:120],
        "class": " ".join(_classes_for(node)[:5])[:160],
        "selector": _selector_for(node)[:160],
        "text": _schema_text(_node_text(node), 240),
        "dismiss_controls": _modal_dismiss_controls(node),
    }


def _modal_overlays(nodes: Iterable[Any]) -> list[dict[str, Any]]:
    overlays: list[dict[str, Any]] = []
    seen_selectors: set[str] = set()
    for node in nodes:
        if len(overlays) >= _MAX_MODAL_OVERLAYS:
            break
        if not _is_modal_overlay_candidate(node):
            continue
        if _is_hidden_modal_candidate(node):
            continue
        selector = _selector_for(node)[:160]
        if selector in seen_selectors:
            continue
        entry = _modal_overlay_entry(node)
        dismiss_controls = entry.get("dismiss_controls")
        has_dismiss_controls = isinstance(dismiss_controls, list) and bool(dismiss_controls)
        has_explicit_modal_semantics = bool(entry.get("role") or entry.get("aria_modal") is True)
        if not (has_explicit_modal_semantics or has_dismiss_controls):
            continue
        seen_selectors.add(selector)
        # Omit aria_modal=False from compact evidence: only affirmative modal
        # semantics matter for obstruction handoff.
        compact_entry = {key: value for key, value in entry.items() if value or key == "dismiss_controls"}
        overlays.append(compact_entry)
    return overlays


def _page_obstructions_from_modal_overlays(modal_overlays: list[dict[str, Any]]) -> list[dict[str, Any]]:
    obstructions: list[dict[str, Any]] = []
    for overlay in modal_overlays[:_MAX_PAGE_OBSTRUCTIONS]:
        if not isinstance(overlay, dict):
            continue
        visible_controls: list[dict[str, Any]] = []
        for control in overlay.get("dismiss_controls") or []:
            if len(visible_controls) >= _MAX_VISIBLE_CONTROLS:
                break
            if not isinstance(control, dict):
                continue
            text = _bounded_string(control.get("text") or control.get("aria_label") or control.get("title"), 120)
            selector = _bounded_string(control.get("selector"), 160)
            if text or selector:
                visible_controls.append(
                    {key: value for key, value in {"text": text, "selector": selector}.items() if value}
                )
        entry = {
            "kind": "modal_overlay",
            "source": DOM_EVIDENCE_SOURCE,
            "selector": _bounded_string(overlay.get("selector"), 160),
            "text": _bounded_string(overlay.get("text"), 240),
            "visible_controls": visible_controls,
        }
        obstructions.append({key: value for key, value in entry.items() if value or key == "visible_controls"})
    return obstructions


def _anti_bot_indicators(html: str, page_title: str) -> list[str]:
    haystack = f"{page_title}\n{html[:_ANTI_BOT_SCAN_BYTES]}".lower()
    return [pattern for pattern in _ANTI_BOT_PATTERNS if pattern in haystack]


def parse_composition_html(html: str, *, inspected_url: str, current_url: str) -> dict[str, Any]:
    """Extract a compact page schema for build-time workflow composition."""

    if BeautifulSoup is None:
        return _empty_evidence(inspected_url, current_url)
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return _empty_evidence(inspected_url, current_url)
    page_title = _page_title(soup)
    challenge_controls = _challenge_controls(soup)
    anti_bot_indicators = _anti_bot_indicators(html or "", page_title)

    for node in soup.find_all(["script", "style", "noscript"]):
        node.decompose()
    for node in soup.find_all(True):
        if node.decomposed:
            continue
        if _is_css_hidden_node(node):
            node.decompose()

    visible_text = _node_text(soup.body if getattr(soup, "body", None) is not None else soup)
    all_nodes = soup.find_all(True)
    modal_overlays = _modal_overlays(all_nodes)
    page_obstructions = _page_obstructions_from_modal_overlays(modal_overlays)
    visual_obstruction_candidates = _visual_obstruction_candidates(soup)

    forms: list[dict[str, Any]] = []
    for form in soup.find_all("form")[:_MAX_FORMS]:
        fields: list[dict[str, Any]] = []
        submit_controls: list[dict[str, Any]] = []
        for node in form.find_all(["input", "select", "textarea", "button"]):
            tag_name = str(getattr(node, "name", "") or "").lower()
            field_type = str(node.get("type") or tag_name or "text").lower()
            if tag_name == "input" and field_type in {"hidden", "reset"}:
                continue
            if tag_name == "button" or field_type in {"submit", "button"}:
                submit_controls.append(
                    {
                        "text": _schema_text(_node_text(node) or str(node.get("value") or ""), 120),
                        "name": str(node.get("name") or "")[:120],
                        "id": str(node.get("id") or "")[:120],
                        "value": _attr_value(node, "value")[:160],
                        "class": " ".join(_classes_for(node)[:5])[:160],
                        "type": field_type[:40],
                        "disabled": _control_disabled(node),
                        "selector": _selector_for(node)[:160],
                    }
                )
                continue
            if len(fields) >= _MAX_FIELDS_PER_FORM:
                continue
            fields.append(
                {
                    "name": str(node.get("name") or "")[:120],
                    "id": str(node.get("id") or "")[:120],
                    "label": _schema_text(_field_label(soup, node), 240),
                    "type": field_type[:40],
                    "value": _attr_value(node, "value")[:160],
                    "class": " ".join(_classes_for(node)[:5])[:160],
                    "placeholder": _schema_text(str(node.get("placeholder") or ""), 240),
                    "required": bool(
                        node.has_attr("required") or str(node.get("aria-required") or "").lower() == "true"
                    ),
                    "disabled": _control_disabled(node),
                    "checked": bool(node.has_attr("checked")),
                    "options": _select_options(node) if tag_name == "select" else [],
                    "selector": _selector_for(node)[:160],
                }
            )
        forms.append(
            {
                "id": str(form.get("id") or "")[:120],
                "name": str(form.get("name") or "")[:120],
                "action": str(form.get("action") or "")[:240],
                "method": str(form.get("method") or "")[:20],
                "fields": fields,
                "submit_controls": submit_controls[:10],
            }
        )

    navigation_targets: list[dict[str, Any]] = []
    for link in soup.find_all("a", href=True):
        if len(navigation_targets) >= _MAX_NAVIGATION_TARGETS:
            break
        href = str(link.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        resolved_href = urljoin(current_url or inspected_url, href)
        if not _same_origin(resolved_href, current_url or inspected_url):
            continue
        text = _node_text(link)
        nav_entry: dict[str, Any] = {
            "text": _schema_text(text, 160),
            "href": resolved_href[:300],
            "selector": _selector_for(link)[:160],
        }
        download_kind = classify_download_affordance(
            href=resolved_href,
            has_download_attr=link.has_attr("download"),
        )
        if download_kind is not None:
            nav_entry[NAV_TARGET_DOWNLOAD_KIND_KEY] = download_kind
        navigation_targets.append(nav_entry)

    result_containers: list[dict[str, Any]] = []
    result_containers_truncated = False
    for node in all_nodes:
        tag_name = str(getattr(node, "name", "") or "").lower()
        node_id = str(node.get("id") or "")
        class_value = node.get("class") or []
        class_text = " ".join(class_value) if isinstance(class_value, list) else str(class_value)
        result_identity = f"{node_id} {class_text}".lower()
        if tag_name == "table" or any(hint in result_identity for hint in _RESULT_CONTAINER_HINTS):
            if len(result_containers) >= _MAX_RESULT_CONTAINERS:
                result_containers_truncated = True
                break
            result_containers.append(_result_container_entry(node, soup=soup))

    key_value_relations, key_value_relations_truncated = _key_value_relations(soup)

    used_selectors: set[str] = set()
    for form in forms:
        for control in form.get("submit_controls") or []:
            selector = control.get("selector")
            if isinstance(selector, str) and selector:
                used_selectors.add(selector)
    for target in navigation_targets:
        selector = target.get("selector")
        if isinstance(selector, str) and selector:
            used_selectors.add(selector)
    clickable_controls = _clickable_controls_html(soup, used_selectors=used_selectors)

    field_count = sum(len(form.get("fields") or []) for form in forms)
    control_count = sum(len(form.get("submit_controls") or []) for form in forms)
    body_text = _node_text(soup.body if soup.body is not None else soup).strip()
    # Schema-empty means the page had content, but no bounded form/link/result/challenge structure.
    schema_empty_page = bool((html or "").strip() or page_title or body_text) and not (
        forms
        or navigation_targets
        or result_containers
        or challenge_controls
        or page_obstructions
        or anti_bot_indicators
    )
    # Higher confidence means the parser saw a more complete form surface.
    confidence = 0.85 if field_count and control_count else 0.6 if field_count else 0.3 if forms else 0.1
    return {
        "inspected_url": inspected_url,
        "current_url": current_url,
        "page_title": page_title,
        "forms": forms,
        "navigation_targets": navigation_targets,
        "result_containers": result_containers,
        "result_containers_truncated": result_containers_truncated,
        "key_value_relations": key_value_relations,
        "key_value_relations_truncated": key_value_relations_truncated,
        **_clickable_controls_channel(clickable_controls),
        "visible_text_excerpt": _schema_text(visible_text, _MAX_VISIBLE_TEXT_EXCERPT_CHARS),
        "anti_bot_indicators": anti_bot_indicators,
        "challenge_controls": challenge_controls,
        "modal_overlays": modal_overlays,
        "page_obstructions": page_obstructions,
        "visual_obstruction_candidates": visual_obstruction_candidates,
        "schema_empty_page": schema_empty_page,
        "observed_empty_page": False,
        "empty_page_visual_state": None,
        "evidence_confidence": confidence,
        "source_tool": "inspect_page_for_composition",
        **_evidence_metadata(anti_bot_indicators, forms=forms, challenge_controls=challenge_controls),
    }


def _structured_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _structured_classes(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    classes = [str(item).strip() for item in value if isinstance(item, str) and str(item).strip()]
    return " ".join(classes[:5])[:160]


def _structured_select_options(value: Any) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return options
    for option in value[:_MAX_SELECT_OPTIONS]:
        if not isinstance(option, dict):
            continue
        options.append(
            {
                "text": _structured_str(option.get("text"))[:120],
                "value": _structured_str(option.get("value")).strip()[:160],
                "selected": option.get("selected") is True,
            }
        )
    return options


def _structured_form(form: Any) -> dict[str, Any] | None:
    if not isinstance(form, dict):
        return None
    fields: list[dict[str, Any]] = []
    for node in form.get("fields") or []:
        if not isinstance(node, dict) or len(fields) >= _MAX_FIELDS_PER_FORM:
            continue
        field_type = (_structured_str(node.get("type")) or "text").lower()
        fields.append(
            {
                "name": _structured_str(node.get("name"))[:120],
                "id": _structured_str(node.get("id"))[:120],
                "label": _schema_text(_structured_str(node.get("label")), 240),
                "type": field_type[:40],
                "value": _structured_str(node.get("value")).strip()[:160],
                "class": _structured_classes(node.get("class")),
                "placeholder": _schema_text(_structured_str(node.get("placeholder")), 240),
                "required": node.get("required") is True,
                "disabled": node.get("disabled") is True,
                "checked": node.get("checked") is True,
                "options": _structured_select_options(node.get("options")),
                "selector": _structured_str(node.get("selector"))[:160],
            }
        )
    submit_controls: list[dict[str, Any]] = []
    for control in form.get("submit_controls") or []:
        if not isinstance(control, dict):
            continue
        submit_controls.append(
            {
                "text": _schema_text(_structured_str(control.get("text")), 120),
                "name": _structured_str(control.get("name"))[:120],
                "id": _structured_str(control.get("id"))[:120],
                "value": _structured_str(control.get("value")).strip()[:160],
                "class": _structured_classes(control.get("class")),
                "type": (_structured_str(control.get("type")) or "").lower()[:40],
                "disabled": control.get("disabled") is True,
                "selector": _structured_str(control.get("selector"))[:160],
            }
        )
    return {
        "id": _structured_str(form.get("id"))[:120],
        "name": _structured_str(form.get("name"))[:120],
        "action": _structured_str(form.get("action"))[:240],
        "method": _structured_str(form.get("method"))[:20],
        "fields": fields,
        "submit_controls": submit_controls[:10],
    }


def _structured_navigation_targets(value: Any, *, base_url: str) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return targets
    for link in value:
        if len(targets) >= _MAX_NAVIGATION_TARGETS:
            break
        if not isinstance(link, dict):
            continue
        href = _structured_str(link.get("href")).strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        if not _same_origin(href, base_url):
            continue
        entry: dict[str, Any] = {
            "text": _schema_text(_structured_str(link.get("text")), 160),
            "href": href[:300],
            "selector": _structured_str(link.get("selector"))[:160],
        }
        download_kind = classify_download_affordance(
            href=href,
            has_download_attr=link.get("has_download_attr") is True,
        )
        if download_kind is not None:
            entry[NAV_TARGET_DOWNLOAD_KIND_KEY] = download_kind
        targets.append(entry)
    return targets


def _structured_result_containers(value: Any) -> list[dict[str, Any]]:
    containers: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return containers
    for node in value:
        if len(containers) >= _MAX_RESULT_CONTAINERS:
            break
        if not isinstance(node, dict):
            continue
        tag_name = (_structured_str(node.get("tag")) or "").lower()
        selector = _structured_str(node.get("selector"))[:160]
        entry: dict[str, Any] = {
            "tag": tag_name,
            "id": _structured_str(node.get("id"))[:120],
            "selector": selector,
            "selector_match_count": node.get("selector_match_count")
            if isinstance(node.get("selector_match_count"), int)
            else 0,
            "visible": node.get("visible") is True,
        }
        headers: list[dict[str, Any]] = []
        raw_headers = node.get("headers")
        if isinstance(raw_headers, list):
            for header in raw_headers[:_MAX_TABLE_HEADERS]:
                if not isinstance(header, dict):
                    continue
                text = _schema_text(_structured_str(header.get("text")), 120)
                column_index = header.get("column_index")
                if not text or not isinstance(column_index, int) or isinstance(column_index, bool) or column_index < 0:
                    continue
                headers.append({"text": text, "column_index": column_index})
        if headers:
            entry["headers"] = headers
        row_count = node.get("row_count")
        if isinstance(row_count, int) and not isinstance(row_count, bool) and row_count >= 0:
            entry["row_count"] = row_count
        entry["rows_truncated"] = node.get("rows_truncated") is True
        entry["span_free"] = node.get("span_free") is True
        entry["nested_table_free"] = node.get("nested_table_free") is True
        rows: list[dict[str, Any]] = []
        raw_rows = node.get("rows")
        if isinstance(raw_rows, list):
            for raw_row in raw_rows[:_MAX_RESULT_SAMPLE_ROWS]:
                if not isinstance(raw_row, dict):
                    continue
                row_index = raw_row.get("row_index")
                if not isinstance(row_index, int) or isinstance(row_index, bool) or row_index < 0:
                    continue
                cells: list[dict[str, Any]] = []
                raw_cells = raw_row.get("cells")
                if isinstance(raw_cells, list):
                    for raw_cell in raw_cells[:_MAX_TABLE_HEADERS]:
                        if not isinstance(raw_cell, dict):
                            continue
                        column_index = raw_cell.get("column_index")
                        if not isinstance(column_index, int) or isinstance(column_index, bool) or column_index < 0:
                            continue
                        cells.append({"column_index": column_index, "visible": raw_cell.get("visible") is True})
                rows.append(
                    {
                        "row_index": row_index,
                        "visible": raw_row.get("visible") is True,
                        "has_row_header": raw_row.get("has_row_header") is True,
                        "cells": cells,
                    }
                )
        entry["rows"] = rows
        sample_rows = [
            _schema_text(_structured_str(row), 240)
            for row in (node.get("sample_rows") or [])
            if isinstance(row, str) and _result_row_text_is_content(row)
        ][:5]
        if sample_rows:
            entry.setdefault("row_count", len(sample_rows))
            entry["sample_rows"] = sample_rows
        text_excerpt = _schema_text(
            _structured_str(
                node.get("text_excerpt") or node.get("content_excerpt") or node.get("sample_text") or node.get("text")
            ),
            240,
        )
        if text_excerpt:
            entry["text_excerpt"] = text_excerpt
        if tag_name == "table" or node.get("is_table") is True:
            reported_row_selector = _structured_str(node.get("row_selector"))[:240]
            entry["row_selector"] = reported_row_selector or f"{selector} tbody tr"
            entry["expand_toggle_candidates"] = [
                f"{selector} tbody tr [aria-expanded]",
                f'{selector} tbody tr [role="button"]',
                f"{selector} tbody tr button",
                f"{selector} tbody tr a",
                f"{selector} tbody tr td:first-child",
            ]
        containers.append(entry)
    return containers


def _structured_key_value_relations(value: Any) -> list[dict[str, Any]]:
    relations: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return relations
    for item in value[:_MAX_KEY_VALUE_RELATIONS]:
        if not isinstance(item, dict):
            continue
        key_text = _schema_text(_structured_str(item.get("key_text")), 120)
        selector = _structured_str(item.get("container_selector"))[:160]
        match_count = item.get("container_match_count")
        position = item.get("container_position")
        child_index = item.get("value_child_index")
        child_count = item.get("direct_child_count")
        if not key_text or not selector:
            continue
        if (
            not isinstance(match_count, int)
            or isinstance(match_count, bool)
            or match_count < 0
            or not isinstance(position, int)
            or isinstance(position, bool)
            or position < 0
            or not isinstance(child_index, int)
            or isinstance(child_index, bool)
            or child_index < 0
            or not isinstance(child_count, int)
            or isinstance(child_count, bool)
            or child_count <= child_index
        ):
            continue
        if match_count <= position:
            continue
        relations.append(
            {
                "key_text": key_text,
                "container_selector": selector,
                "container_match_count": match_count,
                "container_position": position,
                "value_child_index": child_index,
                "direct_child_count": child_count,
                "visible": item.get("visible") is True,
                "value_visible": item.get("value_visible") is True,
            }
        )
    return relations


def _structured_clickable_controls(value: Any) -> list[dict[str, Any]]:
    controls: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return controls
    for item in value[:_MAX_CLICKABLE_CONTROLS]:
        if not isinstance(item, dict):
            continue
        text = _schema_text(_structured_str(item.get("text")), 120)
        selector = _structured_str(item.get("selector"))[:160]
        entry: dict[str, Any] = {}
        if text:
            entry["text"] = text
        if selector:
            entry["selector"] = selector
        tag = (_structured_str(item.get("tag")) or "").lower()[:40]
        if tag:
            entry["tag"] = tag
        if entry.get("selector") or entry.get("text"):
            controls.append(entry)
    return controls


def _structured_challenge_controls(value: Any) -> list[dict[str, Any]]:
    controls: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return controls
    for node in value[:_MAX_CHALLENGE_CONTROLS]:
        if not isinstance(node, dict):
            continue
        entry: dict[str, Any] = {
            "tag": (_structured_str(node.get("tag")) or "").lower(),
            "id": _structured_str(node.get("id"))[:120],
            "name": _structured_str(node.get("name"))[:120],
            "class": _structured_classes(node.get("class")),
            "type": _structured_str(node.get("type"))[:40],
            "selector": _structured_str(node.get("selector"))[:160],
            "text": _schema_text(_structured_str(node.get("text")), 200),
        }
        for key in ("src", "title", "data_sitekey", "data_callback", "data_expired_callback", "data_error_callback"):
            field_value = _structured_str(node.get(key)).strip()
            if field_value:
                entry[key] = field_value[:300]
        controls.append({k: v for k, v in entry.items() if v})
    return controls


def _structured_modal_dismiss_controls(value: Any) -> list[dict[str, Any]]:
    controls: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return controls
    for control in value[:_MAX_MODAL_DISMISS_CONTROLS]:
        if not isinstance(control, dict):
            continue
        entry = {
            "tag": (_structured_str(control.get("tag")) or "").lower()[:40],
            "text": _schema_text(_structured_str(control.get("text")), 120),
            "aria_label": _schema_text(_structured_str(control.get("aria_label")), 120),
            "title": _schema_text(_structured_str(control.get("title")), 120),
            "selector": _structured_str(control.get("selector"))[:160],
            "type": _structured_str(control.get("type"))[:40],
        }
        controls.append({k: v for k, v in entry.items() if v})
    return controls


def _structured_modal_overlays(value: Any) -> list[dict[str, Any]]:
    overlays: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return overlays
    for node in value[:_MAX_MODAL_OVERLAYS]:
        if not isinstance(node, dict):
            continue
        entry = {
            "role": _structured_str(node.get("role"))[:80],
            "aria_modal": node.get("aria_modal") is True,
            "id": _structured_str(node.get("id"))[:120],
            "class": _structured_classes(node.get("class")),
            "selector": _structured_str(node.get("selector"))[:160],
            "text": _schema_text(_structured_str(node.get("text")), 240),
            "dismiss_controls": _structured_modal_dismiss_controls(node.get("dismiss_controls")),
        }
        overlays.append(
            {key: field_value for key, field_value in entry.items() if field_value or key == "dismiss_controls"}
        )
    return overlays


def _structured_visual_obstruction_candidates(value: Any) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return candidates
    for item in value:
        if len(candidates) >= _MAX_PAGE_OBSTRUCTIONS:
            break
        if not isinstance(item, dict):
            continue
        position = item.get("position")
        if position not in {"fixed", "sticky"} or item.get("coverage") != "viewport":
            continue
        candidates.append(
            {
                # computed_style: from getComputedStyle, matching the obstruction-augmentation path.
                "source": "computed_style",
                "position": position,
                "coverage": "viewport",
                "has_visible_controls": item.get("has_visible_controls") is True,
            }
        )
    return candidates


def parse_composition_structured(data: Any, *, inspected_url: str, current_url: str) -> dict[str, Any] | None:
    """Map the structured-evidence JSON to the same PageEvidence dict; None falls back to the get_html path."""
    if not isinstance(data, dict):
        return None
    base_url = current_url or inspected_url
    page_title = _schema_text(_structured_str(data.get("page_title")), 240)
    forms = [form for form in (_structured_form(item) for item in data.get("forms") or []) if form is not None]
    forms = forms[:_MAX_FORMS]
    navigation_targets = _structured_navigation_targets(data.get("navigation_targets"), base_url=base_url)
    result_containers = _structured_result_containers(data.get("result_containers"))
    key_value_relations = _structured_key_value_relations(data.get("key_value_relations"))
    clickable_controls = _structured_clickable_controls(data.get("clickable_controls"))
    challenge_controls = _structured_challenge_controls(data.get("challenge_controls"))
    modal_overlays = _structured_modal_overlays(data.get("modal_overlays"))
    page_obstructions = _page_obstructions_from_modal_overlays(modal_overlays)
    visual_obstruction_candidates = _structured_visual_obstruction_candidates(data.get("visual_obstruction_candidates"))
    visible_text = _schema_text(_structured_str(data.get("visible_text_excerpt")), _MAX_VISIBLE_TEXT_EXCERPT_CHARS)

    # Re-validate JS-reported indicators against _ANTI_BOT_PATTERNS and union a title scan.
    reported = {indicator for indicator in (data.get("anti_bot_indicators") or []) if isinstance(indicator, str)}
    matched = reported | set(_anti_bot_indicators("", page_title))
    anti_bot_indicators = [pattern for pattern in _ANTI_BOT_PATTERNS if pattern in matched]

    field_count = sum(len(form.get("fields") or []) for form in forms)
    control_count = sum(len(form.get("submit_controls") or []) for form in forms)
    # body_has_markup mirrors the HTML path's html.strip() for schema-empty parity.
    body_has_markup = data.get("body_has_markup") is True
    schema_empty_page = bool(body_has_markup or visible_text or page_title) and not (
        forms
        or navigation_targets
        or result_containers
        or challenge_controls
        or page_obstructions
        or anti_bot_indicators
    )
    confidence = 0.85 if field_count and control_count else 0.6 if field_count else 0.3 if forms else 0.1
    return {
        "inspected_url": inspected_url,
        "current_url": current_url,
        "page_title": page_title,
        "forms": forms,
        "navigation_targets": navigation_targets,
        "result_containers": result_containers,
        "result_containers_truncated": data.get("result_containers_truncated") is True,
        "key_value_relations": key_value_relations,
        "key_value_relations_truncated": data.get("key_value_relations_truncated") is True,
        **_clickable_controls_channel(clickable_controls),
        "visible_text_excerpt": visible_text,
        "anti_bot_indicators": anti_bot_indicators,
        "challenge_controls": challenge_controls,
        "modal_overlays": modal_overlays,
        "page_obstructions": page_obstructions,
        "visual_obstruction_candidates": visual_obstruction_candidates,
        "schema_empty_page": schema_empty_page,
        "observed_empty_page": False,
        "empty_page_visual_state": None,
        "evidence_confidence": confidence,
        "source_tool": "inspect_page_for_composition",
        **_evidence_metadata(anti_bot_indicators, forms=forms, challenge_controls=challenge_controls),
    }
