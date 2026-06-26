"""Typed reached-download-target signal for the copilot compose surface, matched on the captured
selector and trajectory recency (never URL identity — a browser download does not change the SPA URL)."""

from __future__ import annotations

import ast
import textwrap
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, cast

DownloadKind = Literal["registered", "attribute", "extension"]

# ``registered`` is the only kind backed by an actual browser download having fired (S1);
# ``attribute``/``extension`` are S2 predictions from a scouted link.
DOWNLOAD_KIND_REGISTERED: DownloadKind = "registered"
DOWNLOAD_KIND_ATTRIBUTE: DownloadKind = "attribute"
DOWNLOAD_KIND_EXTENSION: DownloadKind = "extension"

# S2 may only mint a prediction; ``registered`` is S1-only and must never come from a nav target.
_PREDICTED_DOWNLOAD_KINDS: frozenset[str] = frozenset({DOWNLOAD_KIND_ATTRIBUTE, DOWNLOAD_KIND_EXTENSION})

# File extensions that, when ending an href path, mark the link as a direct file download.
_DOWNLOADABLE_EXTENSIONS: frozenset[str] = frozenset(
    {
        "pdf",
        "csv",
        "tsv",
        "xls",
        "xlsx",
        "xlsm",
        "doc",
        "docx",
        "ppt",
        "pptx",
        "txt",
        "rtf",
        "json",
        "xml",
        "zip",
        "gz",
        "tar",
        "rar",
        "7z",
        "ofx",
        "qfx",
        "qbo",
        "ics",
        "eml",
    }
)

# Block output keys written by the execution-layer download registration when a browser
# download fired inside a code block. Presence of any of these is hard proof of a download.
REGISTERED_DOWNLOAD_OUTPUT_KEYS: tuple[str, ...] = (
    "downloaded_files",
    "downloaded_file_urls",
    "downloaded_file_artifact_ids",
)

# The keys whose typed-affordance hints flow through the scout navTargets capture.
NAV_TARGET_DOWNLOAD_KIND_KEY = "download_kind"


class _SourceStepKind(str, Enum):
    trajectory_recency = "trajectory_recency"
    registered_output = "registered_output"


@dataclass(frozen=True)
class ReachedDownloadTarget:
    selector: str
    affordance_text: str
    download_kind: DownloadKind
    source_step: str
    already_registered: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "selector": self.selector,
            "affordance_text": self.affordance_text,
            "download_kind": self.download_kind,
            "source_step": self.source_step,
            "already_registered": self.already_registered,
        }


def _href_path_extension(href: str) -> str:
    candidate = href.split("?", 1)[0].split("#", 1)[0]
    last_segment = candidate.rsplit("/", 1)[-1]
    if "." not in last_segment:
        return ""
    return last_segment.rsplit(".", 1)[-1].strip().lower()


def classify_download_affordance(*, href: str | None, has_download_attr: bool = False) -> DownloadKind | None:
    """Type a same-host ``<a href>`` as a download affordance, or None if it is plain navigation.

    A ``download`` attribute wins over a downloadable file extension on the href path."""
    if has_download_attr:
        return DOWNLOAD_KIND_ATTRIBUTE
    if href and _href_path_extension(href) in _DOWNLOADABLE_EXTENSIONS:
        return DOWNLOAD_KIND_EXTENSION
    return None


def _summary_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def derive_from_navigation_targets(navigation_targets: Any) -> ReachedDownloadTarget | None:
    """S2: derive a single unambiguous typed download target from scouted navigation targets.

    Returns None when zero or more than one same-host download affordance is present, mirroring
    the auto-act single-survivor rule so an ambiguous page never over-fires the steer."""
    if not isinstance(navigation_targets, list):
        return None
    candidates: list[ReachedDownloadTarget] = []
    for target in navigation_targets:
        if not isinstance(target, dict):
            continue
        download_kind = _summary_str(target.get(NAV_TARGET_DOWNLOAD_KIND_KEY))
        selector = _summary_str(target.get("selector"))
        if download_kind not in _PREDICTED_DOWNLOAD_KINDS or not selector:
            continue
        candidates.append(
            ReachedDownloadTarget(
                selector=selector,
                affordance_text=_summary_str(target.get("text")),
                download_kind=cast(DownloadKind, download_kind),
                source_step=_SourceStepKind.trajectory_recency.value,
                already_registered=False,
            )
        )
    if len(candidates) != 1:
        return None
    return candidates[0]


def block_output_has_registered_download(block_output: Any) -> bool:
    if not isinstance(block_output, dict):
        return False
    return any(bool(block_output.get(key)) for key in REGISTERED_DOWNLOAD_OUTPUT_KEYS)


def derive_from_block_outputs(block_outputs_by_label: Any) -> ReachedDownloadTarget | None:
    """S1: confirm a reached download from a browser download already registered into a block output.

    This is hard proof a download fired; the typed field carries no selector because the affordance
    has already been exercised — the agent only needs to know the run converged on a real download."""
    if not isinstance(block_outputs_by_label, dict):
        return None
    for label, output in block_outputs_by_label.items():
        if block_output_has_registered_download(output):
            return ReachedDownloadTarget(
                selector="",
                affordance_text="",
                download_kind=DOWNLOAD_KIND_REGISTERED,
                source_step=_summary_str(label) or _SourceStepKind.registered_output.value,
                already_registered=True,
            )
    return None


_AUTHOR_DOWNLOAD_GUIDANCE = (
    "A correct click reached a download affordance on the current page. Author ONE terminal "
    "download code block that fires the browser download for the captured target using the "
    "expect_download idiom, not a static-fetch request and not another page re-evaluation. "
    "The downloaded file is registered to the workflow output surface (downloaded_files); never "
    "place file bytes or URLs in the chat reply."
)

_CONFIRMED_DOWNLOAD_GUIDANCE = (
    "A browser download already registered into the workflow output surface (downloaded_files). "
    "The download flow is reached — finalize one terminal download code block rather than "
    "re-evaluating the page or re-authoring static-fetch scout blocks. Never place file bytes or "
    "URLs in the chat reply."
)


def guidance_for(target: ReachedDownloadTarget) -> str:
    return _CONFIRMED_DOWNLOAD_GUIDANCE if target.already_registered else _AUTHOR_DOWNLOAD_GUIDANCE


_EXPECT_DOWNLOAD_ATTR = "expect_download"
_DOWNLOAD_EVENT_CAPTURE_ATTRS: frozenset[str] = frozenset({"wait_for_event", "expect_event"})
_DOWNLOAD_EVENT_NAME = "download"
_REGISTERED_DOWNLOAD_OUTPUT_KEY_SET = frozenset(REGISTERED_DOWNLOAD_OUTPUT_KEYS)


def _call_is_expect_download(node: ast.expr) -> bool:
    if isinstance(node, ast.Await):
        return _call_is_expect_download(node.value)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return node.func.attr == _EXPECT_DOWNLOAD_ATTR
    return False


def _call_is_download_event_capture(node: ast.expr) -> bool:
    """True for the event-based download idioms ``page.wait_for_event("download")`` /
    ``page.expect_event("download")`` (await-unwrapped, first arg the literal ``"download"``).

    A hand-rolled event capture registers the same browser download as ``expect_download`` but evades
    the strict ``expect_download`` predicate, so the gate and contract treat it as download intent."""
    if isinstance(node, ast.Await):
        return _call_is_download_event_capture(node.value)
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    if node.func.attr not in _DOWNLOAD_EVENT_CAPTURE_ATTRS or not node.args:
        return False
    first = node.args[0]
    return isinstance(first, ast.Constant) and first.value == _DOWNLOAD_EVENT_NAME


def _dict_literal_keys(node: ast.expr) -> set[str]:
    if isinstance(node, ast.Await):
        return _dict_literal_keys(node.value)
    if not isinstance(node, ast.Dict):
        return set()
    return {key.value for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)}


def code_is_download_intent(code: str) -> bool:
    """True when a code block authors a download: it uses the `page.expect_download` context-manager
    idiom or the event-based `page.wait_for_event("download")` / `page.expect_event("download")` idiom
    anywhere, or returns/binds a dict literal carrying an execution-layer download registration key.
    Used to require a scout-act before such a block may be authored."""
    if not code.strip():
        return False
    try:
        tree = ast.parse(textwrap.dedent(code).strip() or "pass")
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncWith, ast.With)):
            for item in node.items:
                if _call_is_expect_download(item.context_expr) or _call_is_download_event_capture(item.context_expr):
                    return True
        if isinstance(node, (ast.Call, ast.Await)) and _call_is_download_event_capture(node):
            return True
        if isinstance(node, ast.Return) and node.value is not None:
            if _dict_literal_keys(node.value) & _REGISTERED_DOWNLOAD_OUTPUT_KEY_SET:
                return True
        if isinstance(node, ast.Assign):
            if _dict_literal_keys(node.value) & _REGISTERED_DOWNLOAD_OUTPUT_KEY_SET:
                return True
    return False
