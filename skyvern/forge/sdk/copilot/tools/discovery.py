from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urljoin, urlparse

import structlog
from opentelemetry import trace as otel_trace

try:
    from bs4 import BeautifulSoup  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — bs4 is a transitive dep but discovery degrades gracefully without it.
    BeautifulSoup = None  # type: ignore[assignment, misc]

from skyvern.forge import app
from skyvern.forge.sdk.copilot.build_phase import (
    BuildPhase,
    advance_to_composing,
    advance_to_discovering,
)
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.loop_detection import record_tool_step_result_for_ctx
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span

from ._shared import (
    _DISCOVERY_ANTI_BOT_PATTERNS,
    _DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
    _composition_get_html,
    _discovery_extract_current_url,
    _discovery_navigate,
)
from .guardrails import _authority_tool_error

LOG = structlog.get_logger()

# Build-time entrypoint discovery: navigates and reads pages, returns a
# candidate URL into the agent's context. Never mutates workflow YAML.
# Available only during INITIAL / DISCOVERING phases.

_DISCOVERY_PER_CHAT_BUDGET = 3
_DISCOVERY_PER_TURN_BUDGET = 1
_DISCOVERY_WALL_CLOCK_SECONDS = 60.0
_DISCOVERY_STEP_CAP = 8
_DISCOVERY_EVIDENCE_TRAIL_MAX = 8
_DISCOVERY_CANDIDATE_FORM_FIELDS_MAX = 10
_DISCOVERY_HTML_BYTES_MAX = 200_000
_DISCOVERY_CONCRETE_HOMEPAGE_CONFIDENCE = 0.6

_DISCOVERY_URL_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)
# host + optional path/query/fragment — handles `example.com/login`,
# `the-internet.herokuapp.com/tables?x=y`, `site.com?q=x`, `site.com#frag`.
_DISCOVERY_DOMAIN_WITH_PATH_RE = re.compile(
    r"^[a-z0-9-]+(\.[a-z]{2,})+([/?#][^\s]*)?$",
    re.IGNORECASE,
)
_DISCOVERY_BARE_WORD_RE = re.compile(r"^[a-z0-9-]{2,32}$", re.IGNORECASE)
_DISCOVERY_ALIAS_SEPARATOR_RE = re.compile(r"[\s_-]+")
_DISCOVERY_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_DISCOVERY_CANDIDATE_EVIDENCE_STOPWORDS = frozenset({"a", "an", "and", "for", "in", "of", "on", "or", "the", "to"})
_DISCOVERY_LOGIN_TITLE_RE = re.compile(r"\b(sign\s*in|log\s*in|login)\b", re.IGNORECASE)
_DISCOVERY_PASSWORD_INPUT_RE = re.compile(
    r"<input[^>]*type\s*=\s*[\"']password[\"']",
    re.IGNORECASE,
)


def _normalize_discovery_alias(site_or_url: str) -> str:
    return _DISCOVERY_ALIAS_SEPARATOR_RE.sub("", site_or_url.strip().lower())


def _resolve_discovery_entry_url(site_or_url: str) -> tuple[str | None, str]:
    """Resolve the user-supplied site name/URL into a navigable URL.

    Returns ``(resolved_url, kind)`` where ``kind`` is one of:
    ``url`` / ``domain`` / ``canonical_alias`` / ``word`` / ``unresolved``.
    """
    token = (site_or_url or "").strip()
    if not token:
        return None, "unresolved"
    if _DISCOVERY_URL_SCHEME_RE.match(token):
        return token, "url"
    if _DISCOVERY_DOMAIN_WITH_PATH_RE.match(token):
        return f"https://{token}", "domain"
    alias_resolution = app.AGENT_FUNCTION.resolve_copilot_entrypoint_alias(
        site_or_url=token,
        normalized_alias=_normalize_discovery_alias(token),
    )
    if alias_resolution:
        return alias_resolution.url, alias_resolution.kind
    if _DISCOVERY_BARE_WORD_RE.match(token):
        return f"https://www.{token.lower()}.com", "word"
    return None, "unresolved"


def _concrete_homepage_entrypoint(entry_url: str | None, kind: str) -> str | None:
    if kind not in {"domain", "url"} or not entry_url:
        return None
    try:
        parsed = urlparse(entry_url)
    except Exception:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/"


def _discovery_anchor_score(
    anchor_text: str,
    anchor_title: str,
    href_path: str,
    intent_tokens: set[str],
) -> int:
    """Count intent tokens that appear as substrings of the combined anchor text.

    Substring (not exact-token) matching handles ``sort`` ↔ ``sortable`` and
    ``table`` ↔ ``tables`` without a full stemmer.
    """
    if not intent_tokens:
        return 0
    combined = f"{anchor_text} {anchor_title} {href_path}".lower()
    return sum(1 for token in intent_tokens if token in combined)


def _discovery_title_score(page_title: str, intent_tokens: set[str]) -> int:
    if not intent_tokens or not page_title:
        return 0
    lowered = page_title.lower()
    return sum(1 for token in intent_tokens if token in lowered)


def _discovery_candidate_evidence_tokens(intent_tokens: set[str]) -> set[str]:
    return {
        token
        for token in intent_tokens
        if len(token) > 2 and token.lower() not in _DISCOVERY_CANDIDATE_EVIDENCE_STOPWORDS
    }


def _discovery_detect_login_wall(html: str, page_title: str) -> bool:
    if _DISCOVERY_LOGIN_TITLE_RE.search(page_title or ""):
        return True
    return bool(_DISCOVERY_PASSWORD_INPUT_RE.search(html or ""))


def _discovery_detect_anti_bot(html: str, page_title: str) -> bool:
    lowered_title = (page_title or "").lower()
    lowered_html = (html or "")[:_DISCOVERY_HTML_BYTES_MAX].lower()
    return any(pat in lowered_title or pat in lowered_html for pat in _DISCOVERY_ANTI_BOT_PATTERNS)


def _discovery_build_result(
    *,
    candidate_url: str | None,
    candidate_form_fields: list[dict[str, Any]],
    evidence_trail: list[dict[str, Any]],
    confidence: float,
    failure_reason: str | None,
    ok: bool = True,
    error: str | None = None,
) -> dict[str, Any]:
    """Shape a `discover_workflow_entrypoint` result envelope.

    Convention: ``ok=True`` for any *completed* walk — including controlled
    outcomes that report a ``failure_reason`` and ``candidate_url=None``.
    ``ok=False`` is reserved for actual tool errors (MCP unavailable, browser
    boot failure, internal exception). Matches the existing copilot
    ``_request_policy_tool_error`` convention so the eval harness counts a
    controlled failure as a successful tool call.
    """
    return {
        "ok": ok,
        "data": {
            "candidate_url": candidate_url,
            "candidate_form_fields": candidate_form_fields[:_DISCOVERY_CANDIDATE_FORM_FIELDS_MAX],
            "evidence_trail": evidence_trail[:_DISCOVERY_EVIDENCE_TRAIL_MAX],
            "confidence": float(confidence),
            "failure_reason": failure_reason,
        },
        "error": error,
    }


def _record_discovery_resolution_on_ctx(ctx: Any, result: Mapping[str, Any]) -> None:
    data_payload = result.get("data")
    data: Mapping[str, Any] = data_payload if isinstance(data_payload, Mapping) else {}
    candidate_url = data.get("candidate_url")
    failure_reason = data.get("failure_reason")
    if isinstance(candidate_url, str) and candidate_url:
        prior_candidate_url = getattr(ctx, "resolved_discovery_entrypoint_url", None)
        ctx.resolved_discovery_entrypoint_url = candidate_url
        ctx.resolved_discovery_failure_reason = (
            failure_reason if isinstance(failure_reason, str) and failure_reason else None
        )
        if prior_candidate_url != candidate_url:
            ctx.resolved_discovery_entrypoint_inspection_baseline = int(
                getattr(ctx, "page_inspection_calls_this_turn", 0) or 0
            )
            ctx.discovery_entrypoint_url_question_nudge_count = 0
    # Prior successful candidates remain authoritative over later no-candidate failures.
    elif not getattr(ctx, "resolved_discovery_entrypoint_url", None):
        # No prior candidate and no new one: clear URL state while recording the failure.
        ctx.resolved_discovery_entrypoint_url = None
        ctx.resolved_discovery_failure_reason = (
            failure_reason if isinstance(failure_reason, str) and failure_reason else None
        )
        ctx.resolved_discovery_entrypoint_inspection_baseline = 0
    try:
        current_span = otel_trace.get_current_span()
        if ctx.resolved_discovery_entrypoint_url is not None:
            current_span.set_attribute("copilot.discovery_candidate_url", ctx.resolved_discovery_entrypoint_url)
        if ctx.resolved_discovery_failure_reason is not None:
            current_span.set_attribute("copilot.discovery_failure_reason", ctx.resolved_discovery_failure_reason)
    except Exception:
        LOG.debug("Unable to set discovery resolution span attributes", exc_info=True)


def _discovery_parse_html(html: str) -> tuple[str, list[dict[str, str]], list[dict[str, str]]]:
    """Parse HTML for page title, link anchors, and form-field metadata.

    Uses BeautifulSoup if available (a transitive Skyvern dep). Falls back to
    empty results if not — discovery degrades gracefully rather than crashing.
    """
    if BeautifulSoup is None:
        return "", [], []

    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return "", [], []

    title_text = ""
    title_tag = soup.find("title")
    if title_tag and title_tag.string:
        title_text = title_tag.string.strip()
    h1_tag = soup.find("h1")
    if h1_tag:
        h1_text = h1_tag.get_text(strip=True)
        if h1_text:
            title_text = f"{title_text} {h1_text}".strip()

    anchors: list[dict[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        text = a.get_text(" ", strip=True)
        anchors.append(
            {
                "href": href,
                "text": text[:240],
                "title": (a.get("title") or "")[:240],
            }
        )

    form_fields: list[dict[str, str]] = []
    form = soup.find("form")
    if form is not None:
        for inp in form.find_all(["input", "select", "textarea"]):
            field_type = inp.get("type", inp.name) or "text"
            if field_type.lower() in {"hidden", "submit", "button"}:
                continue
            field_name = inp.get("name") or inp.get("id") or ""
            label_text = ""
            label_id = inp.get("id")
            if label_id:
                label_tag = soup.find("label", attrs={"for": label_id})
                if label_tag is not None:
                    label_text = label_tag.get_text(" ", strip=True)
            form_fields.append(
                {
                    "name": field_name[:120],
                    "label": label_text[:240],
                    "type": str(field_type)[:40],
                    "value_hint": (inp.get("placeholder") or "")[:240],
                }
            )

    return title_text, anchors, form_fields


def _discovery_resolve_href(base_url: str, href: str) -> str | None:
    try:
        absolute = urljoin(base_url, href)
    except Exception:
        return None
    parsed_abs = urlparse(absolute)
    parsed_base = urlparse(base_url)
    if parsed_abs.scheme not in {"http", "https"}:
        return None
    # Same-origin only — discovery does not follow cross-origin links to keep
    # the entrypoint search bounded to the user's named site.
    if parsed_abs.netloc and parsed_base.netloc and parsed_abs.netloc != parsed_base.netloc:
        return None
    return absolute


def _discovery_origin_url(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/"


def _discovery_should_retry_from_origin(entry_url: str, current_url: str) -> bool:
    try:
        parsed_entry = urlparse(entry_url)
        parsed_current = urlparse(current_url)
    except Exception:
        return False
    if parsed_current.scheme not in {"http", "https"} or not parsed_current.netloc:
        return False
    if parsed_entry.netloc and parsed_entry.netloc != parsed_current.netloc:
        return False
    return bool(parsed_current.path not in {"", "/"} or parsed_current.query)


def _discovery_anchor_selector(anchor: dict[str, str]) -> str | None:
    href = (anchor.get("href") or "").strip()
    if not href or any(char in href for char in {'"', "\\", "\n", "\r"}):
        return None
    return f'a[href="{href}"]'


_DISCOVERY_NAVIGATION_FALLBACK_CONFIDENCE = 0.2
# Scorer-miss outcomes (page loaded, no keyword match); wall outcomes are excluded as real blocks.
_DISCOVERY_SCORER_MISS_REASONS = frozenset({"no_candidate", "step_limit", "wall_clock_limit"})


def _discovery_last_loaded_url(evidence_trail: list[dict[str, Any]]) -> str | None:
    """URL of the last page the walk loaded, skipping failed nav/click steps and detected walls."""
    for entry in reversed(evidence_trail):
        reason = str(entry.get("transition_reason", ""))
        if reason.startswith("navigate_failed") or reason.startswith("anchor_click_failed"):
            continue
        if entry.get("wall"):
            continue
        url = entry.get("url")
        if isinstance(url, str) and url:
            return url
    return None


async def _discovery_click_anchor(ctx: CopilotContext, anchor: dict[str, str]) -> dict[str, Any]:
    server = getattr(ctx, "discovery_mcp_server", None)
    if server is None:
        return {"ok": False, "error": "discovery MCP server not attached to context"}
    selector = _discovery_anchor_selector(anchor)
    if selector is None:
        return {"ok": False, "error": "anchor href could not be converted to a bounded CSS selector"}
    try:
        return await asyncio.wait_for(
            # call_internal_tool bypasses the schema overlays, so selector_mode="direct" must be
            # passed explicitly here (it is not picked up from the overlay's forced_args).
            server.call_internal_tool("skyvern_click", {"selector": selector, "selector_mode": "direct"}),
            timeout=_DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"skyvern_click timed out after {_DISCOVERY_PER_CALL_TIMEOUT_SECONDS:g}s"}


async def _discovery_walk(
    ctx: CopilotContext,
    *,
    entry_url: str,
    intent_hint: str,
) -> dict[str, Any]:
    """Deterministic anchor-scoring walker. No inner LLM call.

    Reads each visited page, looks for a strong title/H1 match with the
    intent_hint, surfaces a form if one is present, and otherwise follows
    the highest-scored same-origin anchor whose text matches the
    intent_hint. Bounded by step / wall-clock caps.
    """
    intent_tokens = set(_DISCOVERY_TOKEN_RE.findall(intent_hint.lower())) if intent_hint else set()
    evidence_trail: list[dict[str, Any]] = []
    current_url = entry_url
    current_page_loaded = False
    retried_deep_link_from_origin = False
    started = ctx.discovery_started_monotonic or time.monotonic()

    for step in range(_DISCOVERY_STEP_CAP):
        ctx.discovery_step_count = step + 1
        elapsed = time.monotonic() - started
        if elapsed > _DISCOVERY_WALL_CLOCK_SECONDS:
            return _discovery_build_result(
                candidate_url=None,
                candidate_form_fields=[],
                evidence_trail=evidence_trail,
                confidence=0.0,
                failure_reason="wall_clock_limit",
            )

        if current_page_loaded:
            current_page_loaded = False
        else:
            nav_result = await _discovery_navigate(ctx, current_url)
            if not nav_result.get("ok"):
                evidence_trail.append(
                    {
                        "url": current_url,
                        "page_title": "",
                        "transition_reason": f"navigate_failed: {nav_result.get('error', 'unknown')}"[:240],
                    }
                )
                # A pre-composition browser/session failure is not evidence that
                # the user omitted a page URL. Return the resolved entry URL so the
                # agent can test a minimal goto_url block and gather real run/debug
                # evidence before deciding whether to ask a follow-up.
                return _discovery_build_result(
                    candidate_url=current_url,
                    candidate_form_fields=[],
                    evidence_trail=evidence_trail,
                    confidence=_DISCOVERY_NAVIGATION_FALLBACK_CONFIDENCE,
                    failure_reason=None,
                )

            current_url = _discovery_extract_current_url(nav_result, current_url)
        # Survive the MCP size cap: a heavy DOM exceeds it and the html field is dropped, so
        # fall back to a stripped-body evaluate that keeps the links/forms the resolver needs
        # to identify a usable entrypoint. (Discovery only resolves the entrypoint, so a sliced
        # tail does not matter here.)
        html, _, _, _ = await _composition_get_html(ctx)
        page_title, anchors, form_fields = _discovery_parse_html(html)

        evidence_trail.append(
            {
                "url": current_url,
                "page_title": page_title[:240],
                "transition_reason": "initial" if step == 0 else "anchor_match",
            }
        )

        title_score = _discovery_title_score(page_title, intent_tokens)

        best_score = 0
        best_href: str | None = None
        best_anchor: dict[str, str] | None = None
        for anchor in anchors:
            score = _discovery_anchor_score(
                anchor.get("text", ""),
                anchor.get("title", ""),
                anchor.get("href", ""),
                intent_tokens,
            )
            if score > best_score:
                resolved = _discovery_resolve_href(current_url, anchor.get("href", ""))
                if resolved is None:
                    continue
                best_score = score
                best_href = resolved
                best_anchor = anchor

        evidence_tokens = _discovery_candidate_evidence_tokens(intent_tokens)
        candidate_title_score = _discovery_title_score(page_title, evidence_tokens)
        candidate_anchor_score = 0
        for anchor in anchors:
            candidate_anchor_score = max(
                candidate_anchor_score,
                _discovery_anchor_score(
                    anchor.get("text", ""),
                    anchor.get("title", ""),
                    anchor.get("href", ""),
                    evidence_tokens,
                ),
            )

        anti_bot_detected = _discovery_detect_anti_bot(html, page_title)
        login_wall_detected = _discovery_detect_login_wall(html, page_title)
        anti_bot_has_no_candidate_evidence = (
            not form_fields and candidate_title_score == 0 and candidate_anchor_score == 0
        )
        if anti_bot_detected or login_wall_detected:
            # Tag walls so the scorer-miss fallback refuses them even when they degrade to no_candidate.
            evidence_trail[-1]["wall"] = True
        if anti_bot_detected and anti_bot_has_no_candidate_evidence:
            origin_url = _discovery_origin_url(current_url)
            if (
                not retried_deep_link_from_origin
                and origin_url
                and origin_url != current_url
                and _discovery_should_retry_from_origin(entry_url, current_url)
            ):
                evidence_trail[-1]["transition_reason"] = "direct_deep_link_anti_bot"
                current_url = origin_url
                retried_deep_link_from_origin = True
                continue
            return _discovery_build_result(
                candidate_url=None,
                candidate_form_fields=[],
                evidence_trail=evidence_trail,
                confidence=0.0,
                failure_reason="anti_bot_wall",
            )
        if login_wall_detected:
            return _discovery_build_result(
                candidate_url=None,
                candidate_form_fields=[],
                evidence_trail=evidence_trail,
                confidence=0.0,
                failure_reason="login_wall",
            )

        if intent_tokens and title_score >= 2 and (form_fields or best_score <= title_score):
            confidence = min(1.0, title_score / max(1, len(intent_tokens)))
            return _discovery_build_result(
                candidate_url=current_url,
                candidate_form_fields=form_fields,
                evidence_trail=evidence_trail,
                confidence=confidence,
                failure_reason=None,
            )

        if form_fields and (title_score >= 1 or step > 0):
            confidence = 0.6 if title_score >= 1 else 0.4
            return _discovery_build_result(
                candidate_url=current_url,
                candidate_form_fields=form_fields,
                evidence_trail=evidence_trail,
                confidence=confidence,
                failure_reason=None,
            )

        if not intent_tokens:
            return _discovery_build_result(
                candidate_url=current_url,
                candidate_form_fields=form_fields,
                evidence_trail=evidence_trail,
                confidence=0.3,
                failure_reason=None,
            )

        if best_score == 0 or best_href is None:
            return _discovery_build_result(
                candidate_url=None,
                candidate_form_fields=[],
                evidence_trail=evidence_trail,
                confidence=0.0,
                failure_reason="no_candidate",
            )

        if retried_deep_link_from_origin and best_anchor is not None:
            click_result = await _discovery_click_anchor(ctx, best_anchor)
            if click_result.get("ok"):
                current_url = _discovery_extract_current_url(click_result, best_href)
                # The next loop should inspect the clicked page instead of
                # navigating back to the original entry URL.
                current_page_loaded = True
                continue
            evidence_trail.append(
                {
                    "url": best_href,
                    "page_title": "",
                    "transition_reason": f"anchor_click_failed: {click_result.get('error', 'unknown')}"[:240],
                }
            )

        current_url = best_href

    return _discovery_build_result(
        candidate_url=None,
        candidate_form_fields=[],
        evidence_trail=evidence_trail,
        confidence=0.0,
        failure_reason="step_limit",
    )


async def _discover_workflow_entrypoint_impl(
    copilot_ctx: Any,
    site_or_url: str,
    intent_hint: str,
) -> dict[str, Any]:
    """Discovery tool body — separated from the @function_tool wrapper so
    tests can drive it with a stand-in ctx without the SDK's invocation
    machinery.
    """
    arguments = {"site_or_url": site_or_url, "intent_hint": intent_hint}

    def finish(result: dict[str, Any], *, site_or_url_kind: str | None = None) -> dict[str, Any]:
        _record_discovery_resolution_on_ctx(copilot_ctx, result)
        record_tool_step_result_for_ctx(copilot_ctx, "discover_workflow_entrypoint", arguments, result)
        data_payload = result.get("data")
        data = data_payload if isinstance(data_payload, Mapping) else {}
        candidate_url = data.get("candidate_url")
        failure_reason = data.get("failure_reason")
        if not isinstance(failure_reason, str) or not failure_reason:
            error = result.get("error")
            failure_reason = error if isinstance(error, str) and error else None
        LOG.info(
            "discover_workflow_entrypoint completed",
            ok=result.get("ok"),
            candidate_url=candidate_url if isinstance(candidate_url, str) and candidate_url else None,
            failure_reason=failure_reason,
            site_or_url_kind=site_or_url_kind,
        )
        return result

    authority_error = _authority_tool_error(copilot_ctx, "discover_workflow_entrypoint")
    if authority_error:
        result = {"ok": False, "error": authority_error}
        return finish(result)

    if copilot_ctx.discovery_calls_this_turn >= _DISCOVERY_PER_TURN_BUDGET:
        result = _discovery_build_result(
            candidate_url=None,
            candidate_form_fields=[],
            evidence_trail=list(copilot_ctx.discovery_evidence_trail),
            confidence=0.0,
            failure_reason="discovery_already_completed_this_turn",
        )
        return finish(result)

    cumulative = copilot_ctx.prior_discovery_calls_made + copilot_ctx.discovery_calls_this_turn
    if cumulative >= _DISCOVERY_PER_CHAT_BUDGET:
        result = _discovery_build_result(
            candidate_url=None,
            candidate_form_fields=[],
            evidence_trail=[],
            confidence=0.0,
            failure_reason="discovery_budget_exhausted_for_chat",
        )
        return finish(result)

    entry_url, kind = _resolve_discovery_entry_url(site_or_url)
    if entry_url is None:
        result = _discovery_build_result(
            candidate_url=None,
            candidate_form_fields=[],
            evidence_trail=[],
            confidence=0.0,
            failure_reason="could_not_resolve_site_name",
        )
        return finish(result, site_or_url_kind=kind)

    if copilot_ctx.build_phase == BuildPhase.INITIAL:
        try:
            advance_to_discovering(copilot_ctx)
        except ValueError as exc:
            # Race or unexpected prior advance — proceed without re-transitioning,
            # but surface the impossible state so it shows up in production logs.
            LOG.warning(
                "discover_workflow_entrypoint phase transition to discovering rejected",
                error=str(exc),
                build_phase=copilot_ctx.build_phase.value,
            )
    copilot_ctx.discovery_calls_this_turn += 1

    concrete_homepage_url = _concrete_homepage_entrypoint(entry_url, kind)
    if concrete_homepage_url is not None:
        evidence_trail = [
            {
                "url": concrete_homepage_url,
                "page_title": "",
                "transition_reason": "concrete_domain_homepage",
            }
        ]
        result = _discovery_build_result(
            candidate_url=concrete_homepage_url,
            candidate_form_fields=[],
            evidence_trail=evidence_trail,
            # Lower than a scraped-page match because the fast path skips page inspection.
            confidence=_DISCOVERY_CONCRETE_HOMEPAGE_CONFIDENCE,
            failure_reason=None,
        )
        copilot_ctx.discovery_evidence_trail = list(evidence_trail)
        try:
            advance_to_composing(copilot_ctx, reason="discovery_concrete_domain_homepage")
        except ValueError as exc:
            LOG.warning(
                "discover_workflow_entrypoint phase transition to composing rejected",
                error=str(exc),
                build_phase=copilot_ctx.build_phase.value,
            )
        return finish(result, site_or_url_kind=kind)

    with copilot_span(
        "discover_workflow_entrypoint",
        data={
            "site_or_url_kind": kind,
            "intent_hint_len": len(intent_hint or ""),
            "phase_entered": copilot_ctx.build_phase.value,
        },
    ):
        try:
            result = await _discovery_walk(
                copilot_ctx,
                entry_url=entry_url,
                intent_hint=intent_hint or "",
            )
        except Exception as exc:
            LOG.exception("discover_workflow_entrypoint walker raised")
            result = {
                "ok": False,
                "data": {
                    "candidate_url": None,
                    "candidate_form_fields": [],
                    "evidence_trail": [],
                    "confidence": 0.0,
                    "failure_reason": None,
                },
                "error": f"discover_workflow_entrypoint failed: {exc}",
            }

    data_payload = result.get("data") or {}
    data: dict[str, Any] = data_payload if isinstance(data_payload, dict) else {}
    evidence_trail = data.get("evidence_trail") or []
    copilot_ctx.discovery_evidence_trail = list(evidence_trail)
    if (
        result.get("ok")
        and not data.get("candidate_url")
        and data.get("failure_reason") in _DISCOVERY_SCORER_MISS_REASONS
    ):
        navigated_url = _discovery_last_loaded_url(evidence_trail)
        if navigated_url:
            # Don't ask for a URL we already reached; hand the loaded page to composition at low confidence.
            result = _discovery_build_result(
                candidate_url=navigated_url,
                candidate_form_fields=[],
                evidence_trail=evidence_trail,
                confidence=_DISCOVERY_NAVIGATION_FALLBACK_CONFIDENCE,
                failure_reason=None,
            )
            rebuilt_data = result.get("data")
            data = rebuilt_data if isinstance(rebuilt_data, dict) else {}
            copilot_ctx.discovery_evidence_trail = list(data.get("evidence_trail", []))
    if result.get("ok") and data.get("candidate_url"):
        try:
            advance_to_composing(copilot_ctx, reason="discovery_returned_candidate")
        except ValueError as exc:
            LOG.warning(
                "discover_workflow_entrypoint phase transition to composing rejected",
                error=str(exc),
                build_phase=copilot_ctx.build_phase.value,
            )

    return finish(result, site_or_url_kind=kind)
