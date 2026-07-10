from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from skyvern.forge.sdk.copilot.completion_output_grounding import _boundary_delimited_present
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.submission.models import (
    BrowserPath,
    CandidateWindow,
    DownloadEvidence,
    NetworkEvidence,
    PageConfirmationEvidence,
    SubmissionSignal,
    SubmissionVerdict,
    SubmitCandidate,
    SubmitCandidateDetection,
    TierAEvaluation,
    TierBEvaluation,
    TierBEvidence,
    UrlTransitionEvidence,
)
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import Action, ClickAction
from skyvern.webeye.actions.responses import ActionResult

_SUBMIT_VERBS = ("place order", "register", "complete", "confirm", "finish", "submit", "apply", "send")
_CONFIRMATION_PHRASES = (
    "confirmation number",
    "successfully submitted",
    "application received",
    "submission received",
    "we have received",
    "thank you",
)
_NETWORK_METHODS = {"POST", "PUT", "PATCH"}
_NETWORK_RESOURCE_TYPES = {"document", "xhr", "fetch"}
_STATIC_MIME_PREFIXES = ("image/", "font/", "text/css")
_STATIC_EXTENSIONS = {
    ".avif",
    ".bmp",
    ".css",
    ".eot",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".map",
    ".otf",
    ".png",
    ".svg",
    ".ttf",
    ".webp",
    ".woff",
    ".woff2",
}
_STEP_WINDOW_SKEW = timedelta(seconds=2)
_STEP_WINDOW_TRAILING = timedelta(seconds=5)
_CONFIRMATION_VALUE_RE = re.compile(r"^[\s:;#-]*([^\s<>,;]+)")


@dataclass(frozen=True)
class CandidateEvaluation:
    step_id: str
    tier_a: Sequence[NetworkEvidence]
    tier_b: TierBEvaluation
    is_latest: bool = False


def classify_browser_path(
    *,
    browser_session_id: str | None,
    task_browser_session_id: str | None,
    remote_browser_session_id: str | None,
    task_browser_address: str | None,
    needs_cdp_frame_publisher: bool,
    browser_type: str,
) -> BrowserPath:
    if browser_session_id or task_browser_session_id:
        return BrowserPath.SESSION_ATTACHED
    if remote_browser_session_id:
        return BrowserPath.VENDOR_REUSED
    if task_browser_address or needs_cdp_frame_publisher or browser_type == "cdp-connect":
        return BrowserPath.CDP_CONNECT
    if browser_type in {"chromium-headless", "chromium-headful"}:
        return BrowserPath.SKYVERN_CREATED
    return BrowserPath.UNKNOWN


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _submit_shaped(element_data: Mapping[str, object]) -> bool:
    tag_name = element_data.get("tagName")
    if not isinstance(tag_name, str):
        return False
    tag_name = tag_name.strip().lower()
    attributes = _mapping(element_data.get("attributes")) or {}
    input_type = attributes.get("type")
    if tag_name in {"button", "input"} and isinstance(input_type, str) and input_type.lower() == "submit":
        return True
    if tag_name not in {"button", "input", "a"}:
        return False

    text_values = [element_data.get("text"), attributes.get("value"), attributes.get("aria-label")]
    return any(
        _boundary_delimited_present(verb, value.strip().lower())
        for value in text_values
        if isinstance(value, str)
        for verb in _SUBMIT_VERBS
    )


def detect_submit_candidates(actions: Sequence[Action]) -> SubmitCandidateDetection:
    candidates: list[SubmitCandidate] = []
    coordinate_click = False
    for action in actions:
        if action.action_type != ActionType.CLICK:
            continue
        if isinstance(action, ClickAction) and (action.x is not None or action.y is not None):
            coordinate_click = True
            continue
        element_data = _mapping(action.skyvern_element_data)
        if element_data is not None and _submit_shaped(element_data):
            if action.step_id is None:
                continue
            candidates.append(
                SubmitCandidate(
                    action_id=action.action_id,
                    task_id=action.task_id,
                    step_id=action.step_id,
                )
            )
    return SubmitCandidateDetection(candidates=candidates, coordinate_click=coordinate_click)


def _candidate_actions(
    detection_or_candidates: SubmitCandidateDetection | Sequence[SubmitCandidate],
) -> Sequence[SubmitCandidate]:
    if isinstance(detection_or_candidates, SubmitCandidateDetection):
        return detection_or_candidates.candidates
    return detection_or_candidates


def build_candidate_windows(
    detection_or_candidates: SubmitCandidateDetection | Sequence[SubmitCandidate],
    steps: Sequence[Step],
) -> list[CandidateWindow]:
    step_by_id = {step.step_id: step for step in steps}
    windows: list[CandidateWindow] = []
    seen_steps: set[str] = set()
    for candidate in _candidate_actions(detection_or_candidates):
        if candidate.step_id is None or candidate.step_id in seen_steps:
            continue
        step = step_by_id.get(candidate.step_id)
        if step is None or (candidate.task_id is not None and candidate.task_id != step.task_id):
            continue
        seen_steps.add(step.step_id)
        windows.append(
            CandidateWindow(
                task_id=step.task_id,
                step_id=step.step_id,
                candidate_action_id=candidate.action_id,
                started_at=step.created_at - _STEP_WINDOW_SKEW,
                ended_at=step.modified_at + _STEP_WINDOW_TRAILING,
            )
        )
    return sorted(windows, key=lambda window: window.started_at)


def find_candidate_step_pairs(
    detection_or_candidates: SubmitCandidateDetection | Sequence[SubmitCandidate],
    steps: Sequence[Step],
) -> list[tuple[Step, Step | None]]:
    ordered = sorted(steps, key=lambda step: (step.task_id, step.order, step.retry_index, step.created_at))
    positions = {step.step_id: index for index, step in enumerate(ordered)}
    pairs: list[tuple[Step, Step | None]] = []
    seen_steps: set[str] = set()
    for candidate in _candidate_actions(detection_or_candidates):
        if candidate.step_id is None or candidate.step_id in seen_steps or candidate.step_id not in positions:
            continue
        pre_step = ordered[positions[candidate.step_id]]
        if candidate.task_id is not None and candidate.task_id != pre_step.task_id:
            continue
        seen_steps.add(candidate.step_id)
        index = positions[pre_step.step_id]
        post_step = ordered[index + 1] if index + 1 < len(ordered) else None
        if post_step is not None and post_step.task_id != pre_step.task_id:
            post_step = None
        pairs.append((pre_step, post_step))
    return sorted(pairs, key=lambda pair: pair[0].modified_at)


def find_latest_candidate_step_pair(
    detection_or_candidates: SubmitCandidateDetection | Sequence[SubmitCandidate],
    steps: Sequence[Step],
) -> tuple[Step | None, Step | None]:
    pairs = find_candidate_step_pairs(detection_or_candidates, steps)
    if not pairs:
        return None, None
    return pairs[-1]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_har_time(value: str) -> datetime | None:
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _origin(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        if not parsed.scheme or parsed.hostname is None:
            return None
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme.lower()}://{parsed.hostname.lower()}{port}"
    except ValueError:
        return None


def _path(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if not parsed.scheme or parsed.hostname is None:
        return None
    return parsed.path or "/"


def _content_type(response: Mapping[str, object]) -> str:
    content = _mapping(response.get("content"))
    mime_type = content.get("mimeType") if content is not None else None
    if isinstance(mime_type, str):
        return mime_type.lower()
    headers = response.get("headers")
    if isinstance(headers, list):
        for header in headers:
            header_data = _mapping(header)
            if header_data is None:
                continue
            name = header_data.get("name")
            value = header_data.get("value")
            if isinstance(name, str) and name.lower() == "content-type" and isinstance(value, str):
                return value.lower()
    return ""


def _is_static(entry: Mapping[str, object], response: Mapping[str, object], url: str) -> bool:
    resource_type = entry.get("_resourceType")
    if isinstance(resource_type, str) and resource_type:
        return resource_type.lower() not in _NETWORK_RESOURCE_TYPES
    content_type = _content_type(response)
    if any(content_type.startswith(prefix) for prefix in _STATIC_MIME_PREFIXES):
        return True
    return PurePosixPath(urlsplit(url).path.lower()).suffix in _STATIC_EXTENSIONS


def evaluate_tier_a(har_bytes: bytes, candidate_windows: Sequence[CandidateWindow]) -> TierAEvaluation:
    har_present = bool(har_bytes)
    if not har_present:
        return TierAEvaluation(evidence=[], har_present=False, har_parsed=False, har_entry_count=0)
    try:
        payload = json.loads(har_bytes)
        log = _mapping(payload.get("log")) if isinstance(payload, Mapping) else None
        entries = log.get("entries") if log is not None else None
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        entries = None
    if not isinstance(entries, list):
        return TierAEvaluation(evidence=[], har_present=True, har_parsed=False, har_entry_count=0)

    evidence: list[NetworkEvidence] = []
    ambiguous_entry_count = 0
    for entry_value in entries:
        entry = _mapping(entry_value)
        request = _mapping(entry.get("request")) if entry is not None else None
        response = _mapping(entry.get("response")) if entry is not None else None
        started_value = entry.get("startedDateTime") if entry is not None else None
        if entry is None or request is None or response is None or not isinstance(started_value, str):
            return TierAEvaluation(evidence=[], har_present=True, har_parsed=False, har_entry_count=len(entries))
        method = request.get("method")
        url = request.get("url")
        status = response.get("status")
        started_at = _parse_har_time(started_value)
        origin = _origin(url) if isinstance(url, str) else None
        if (
            not isinstance(method, str)
            or not isinstance(url, str)
            or not isinstance(status, int)
            or isinstance(status, bool)
            or started_at is None
            or origin is None
        ):
            return TierAEvaluation(evidence=[], har_present=True, har_parsed=False, har_entry_count=len(entries))
        if method.upper() not in _NETWORK_METHODS or not 200 <= status < 400 or _is_static(entry, response, url):
            continue
        matching_windows = [
            window for window in candidate_windows if _utc(window.started_at) <= started_at <= _utc(window.ended_at)
        ]
        if len(matching_windows) > 1:
            ambiguous_entry_count += 1
            continue
        if len(matching_windows) == 1:
            evidence.append(
                NetworkEvidence(
                    origin=origin,
                    method=method.upper(),
                    status=status,
                    started_at=started_at,
                    correlated_step_id=matching_windows[0].step_id,
                )
            )
    return TierAEvaluation(
        evidence=evidence,
        har_present=True,
        har_parsed=True,
        har_entry_count=len(entries),
        ambiguous_entry_count=ambiguous_entry_count,
    )


def _confirmation_value(phrase: str, normalized_post_text: str) -> str:
    if phrase != "confirmation number":
        return phrase
    index = normalized_post_text.find(phrase)
    match = _CONFIRMATION_VALUE_RE.match(normalized_post_text[index + len(phrase) :])
    return match.group(1) if match else phrase


def evaluate_tier_b(
    *,
    pre_url: str | None,
    post_url: str | None,
    action_results: Sequence[ActionResult],
    pre_page_text: str | None,
    post_page_text: str | None,
) -> TierBEvaluation:
    evidence: list[TierBEvidence] = []
    if pre_url is not None and post_url is not None:
        from_origin = _origin(pre_url)
        to_origin = _origin(post_url)
        from_path = _path(pre_url)
        to_path = _path(post_url)
        if from_origin is not None and to_origin is not None and from_path is not None and to_path is not None:
            path_changed = from_path != to_path
            if from_origin != to_origin or path_changed:
                evidence.append(
                    UrlTransitionEvidence(
                        from_origin=from_origin,
                        to_origin=to_origin,
                        path_changed=path_changed,
                    )
                )

    file_count = sum(
        max(len(result.downloaded_files or []), 1 if result.download_triggered else 0) for result in action_results
    )
    if file_count:
        evidence.append(DownloadEvidence(file_count=file_count))

    page_evaluated = False
    if pre_page_text is not None and post_page_text is not None:
        page_evaluated = True
        normalized_pre = pre_page_text.casefold()
        normalized_post = post_page_text.casefold()
        for phrase in _CONFIRMATION_PHRASES:
            if _boundary_delimited_present(phrase, normalized_post) and not _boundary_delimited_present(
                phrase, normalized_pre
            ):
                matched_value = _confirmation_value(phrase, normalized_post)
                evidence.append(
                    PageConfirmationEvidence(
                        phrase=phrase,
                        value_sha256=hashlib.sha256(matched_value.encode()).hexdigest(),
                        absent_pre_submit=True,
                    )
                )
                break
    return TierBEvaluation(evidence=evidence, page_confirmation_evaluated=page_evaluated)


def combine(
    *,
    tier_a: TierAEvaluation,
    candidate_evaluations: Sequence[CandidateEvaluation],
    detected_candidate_step_ids: Sequence[str],
    submit_intent_detected: bool,
    browser_path: BrowserPath,
    cua_run: bool = False,
    coordinate_click: bool = False,
) -> SubmissionVerdict:
    tier_a_allowed = browser_path == BrowserPath.SKYVERN_CREATED

    def candidate_strength(candidate: CandidateEvaluation) -> int:
        has_tier_a = tier_a_allowed and bool(candidate.tier_a)
        has_tier_b = bool(candidate.tier_b.evidence)
        if has_tier_a and has_tier_b:
            return 2
        return int(has_tier_a or has_tier_b)

    winning_candidate = max(
        candidate_evaluations,
        key=lambda candidate: (candidate_strength(candidate), candidate.is_latest),
        default=None,
    )
    tier_a_evidence = list(winning_candidate.tier_a) if winning_candidate is not None and tier_a_allowed else []
    tier_b_evidence = list(winning_candidate.tier_b.evidence) if winning_candidate is not None else []
    winning_strength = candidate_strength(winning_candidate) if winning_candidate is not None else 0
    capped = False
    notes: list[str] = []
    if tier_a.ambiguous_entry_count:
        notes.append(f"ambiguous_entries:{tier_a.ambiguous_entry_count}")

    if cua_run or coordinate_click:
        signal = SubmissionSignal.NOT_EVALUATED
        notes.append("cua_or_coordinate_click")
    elif not submit_intent_detected:
        signal = SubmissionSignal.NOT_EVALUATED
        notes.append("submit_intent_not_detected")
    elif browser_path != BrowserPath.SKYVERN_CREATED:
        capped = True
        signal = SubmissionSignal.SUBMITTED_LIKELY if winning_strength else SubmissionSignal.NOT_EVALUATED
        notes.append("tier_b_only_browser_path")
    elif winning_strength == 2:
        signal = SubmissionSignal.SUBMITTED_VERIFIED
    elif winning_strength == 1:
        signal = SubmissionSignal.SUBMITTED_LIKELY
    elif (
        tier_a.har_present
        and tier_a.har_parsed
        and tier_a.har_entry_count > 0
        # An ambiguous entry is a discarded, possibly real submission POST — absence is not affirmative.
        and tier_a.ambiguous_entry_count == 0
        and len(candidate_evaluations) > 0
        # A detected candidate whose step could not be mapped (actions.step_id has no FK) is unknown
        # evidence — evaluations must cover every detected candidate step.
        and set(detected_candidate_step_ids) <= {candidate.step_id for candidate in candidate_evaluations}
        # Every candidate's page-confirmation check must have actually run, not just the latest one's.
        and all(candidate.tier_b.page_confirmation_evaluated for candidate in candidate_evaluations)
        and all(not candidate.tier_a and not candidate.tier_b.evidence for candidate in candidate_evaluations)
    ):
        signal = SubmissionSignal.NOT_SUBMITTED
    else:
        signal = SubmissionSignal.NOT_EVALUATED

    return SubmissionVerdict(
        signal=signal,
        tier_a=tier_a_evidence,
        tier_b=tier_b_evidence,
        submit_intent_detected=submit_intent_detected,
        har_present=tier_a.har_present,
        har_parsed=tier_a.har_parsed,
        har_entry_count=tier_a.har_entry_count,
        browser_path=browser_path,
        winning_step_id=winning_candidate.step_id if winning_candidate is not None else None,
        capped=capped,
        notes=notes,
    )
