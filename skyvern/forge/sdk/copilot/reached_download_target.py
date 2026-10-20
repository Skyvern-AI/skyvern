"""Typed reached-download-target signal for the copilot compose surface, matched on the captured
selector and trajectory recency (never URL identity — a browser download does not change the SPA URL)."""

from __future__ import annotations

import ast
import textwrap
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, cast

DownloadKind = Literal["registered", "attribute", "extension", "observed", "observed_render"]

# ``registered`` (S1) and ``observed`` (S3) are backed by an actual browser download having fired;
# ``attribute``/``extension`` are S2 predictions from a scouted link's href shape.
DOWNLOAD_KIND_REGISTERED: DownloadKind = "registered"
DOWNLOAD_KIND_ATTRIBUTE: DownloadKind = "attribute"
DOWNLOAD_KIND_EXTENSION: DownloadKind = "extension"
# A download the scout's own click produced. Href shape cannot see these: a query-parameter URL
# serving a file via Content-Disposition has no extension and no ``download`` attribute, so the
# href-shape prediction is blind to it and only the fired download proves the affordance.
DOWNLOAD_KIND_OBSERVED: DownloadKind = "observed"
# A document the scout's click opened as an inline render — a PDF statement handed to the browser's
# viewer, or a bill image. No browser download event fires for these, so the dir-diff proof above
# cannot see them; the popup whose response *is* the document is the equivalent proof.
DOWNLOAD_KIND_OBSERVED_RENDER: DownloadKind = "observed_render"

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

REGISTERED_DOWNLOAD_REQUESTED_OUTPUT_PATHS: frozenset[str] = frozenset(
    f"output.{key}" for key in REGISTERED_DOWNLOAD_OUTPUT_KEYS
)

# Written by the secure CodeBlock worker: DOWNLOAD artifacts the block generated itself. They are real
# run downloads, but never proof that a site served a file.
GENERATED_FILE_ARTIFACT_IDS_KEY = "generated_file_artifact_ids"

# Nested mapping the completion grader also reads registration keys from; a strip that covered only
# the root would leave `{"output": {"downloaded_files": [...]}}` gradeable as a real download.
_NESTED_DOWNLOAD_OUTPUT_KEY = "output"


def strip_block_authored_download_keys(output: dict[str, Any] | list | str | None) -> list[str]:
    """Drop registration keys a block authored, returning the dotted names removed; the scopes mirror
    ``_completion_evidence_payload`` — the root mapping and one nested ``output`` mapping — because only
    the execution layer's storage read-back may populate these keys.

    Only a truthy value is a claim, matching ``bind_downloaded_files_to_output``: an empty field is
    schema a strict-rendering template may dereference, and the two engines must drop the same keys."""
    dropped: list[str] = []
    if not isinstance(output, dict):
        return dropped
    scopes: list[tuple[str, dict[str, Any]]] = [("", output)]
    nested = output.get(_NESTED_DOWNLOAD_OUTPUT_KEY)
    if isinstance(nested, dict):
        scopes.append((f"{_NESTED_DOWNLOAD_OUTPUT_KEY}.", nested))
    for prefix, mapping in scopes:
        for key in REGISTERED_DOWNLOAD_OUTPUT_KEYS:
            if mapping.get(key):
                mapping.pop(key)
                dropped.append(f"{prefix}{key}")
    return dropped


# The keys whose typed-affordance hints flow through the scout navTargets capture.
NAV_TARGET_DOWNLOAD_KIND_KEY = "download_kind"


class _SourceStepKind(str, Enum):
    trajectory_recency = "trajectory_recency"
    registered_output = "registered_output"
    observed_download = "observed_download"
    observed_render = "observed_render"


@dataclass(frozen=True)
class ReachedDownloadTarget:
    selector: str
    affordance_text: str
    download_kind: DownloadKind
    source_step: str
    already_registered: bool
    # Stored ``trajectory_index`` of the last scouted interaction at the moment the affordance was
    # observed; the synthesizer sequences the download terminal here instead of after the whole trajectory.
    trajectory_anchor: int | None = None
    # For ``observed_render`` only: the click-proven URL the popup rendered. Provenance is the
    # scout's own click, never a model guess, which is what licenses saving from it.
    rendered_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "selector": self.selector,
            "affordance_text": self.affordance_text,
            "download_kind": self.download_kind,
            "source_step": self.source_step,
            "already_registered": self.already_registered,
            "rendered_url": self.rendered_url,
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


def generated_file_artifact_ids(outputs: Iterable[Any]) -> frozenset[str]:
    """Worker-stamped ids over every raw block row; a label-keyed map drops a looped block's earlier iterations."""
    return frozenset(
        artifact_id
        for output in outputs
        if isinstance(output, dict) and isinstance(ids := output.get(GENERATED_FILE_ARTIFACT_IDS_KEY), list)
        for artifact_id in ids
        if isinstance(artifact_id, str)
    )


def without_generated_file_stamp(output: Any) -> Any:
    """``output`` without the worker-owned key, for a code block row whose output the secure worker did not write."""
    if not isinstance(output, dict) or GENERATED_FILE_ARTIFACT_IDS_KEY not in output:
        return output
    return {key: value for key, value in output.items() if key != GENERATED_FILE_ARTIFACT_IDS_KEY}


def registered_download_proof_view(output: Any, generated: frozenset[str]) -> Any:
    """``output`` with the rows of ``generated`` removed from its registration keys."""
    if not isinstance(output, dict) or not generated:
        return output

    def is_generated(value: Any) -> bool:
        artifact_id = value.get("artifact_id") if isinstance(value, dict) else value
        return isinstance(artifact_id, str) and artifact_id in generated

    view = dict(output)
    files = output.get("downloaded_files")
    kept = [not is_generated(item) for item in files] if isinstance(files, list) else []
    if isinstance(files, list):
        view["downloaded_files"] = [item for item, keep in zip(files, kept) if keep]
    urls = output.get("downloaded_file_urls")
    if isinstance(urls, list):
        # URLs carry no artifact id; binding writes them parallel to downloaded_files.
        view["downloaded_file_urls"] = [url for url, keep in zip(urls, kept) if keep] if len(kept) == len(urls) else []
    artifact_ids = output.get("downloaded_file_artifact_ids")
    if isinstance(artifact_ids, list):
        view["downloaded_file_artifact_ids"] = [item for item in artifact_ids if not is_generated(item)]
    return view


def block_output_has_registered_download(block_output: Any, generated: frozenset[str] = frozenset()) -> bool:
    view = registered_download_proof_view(block_output, generated)
    if not isinstance(view, dict):
        return False
    return any(bool(view.get(key)) for key in REGISTERED_DOWNLOAD_OUTPUT_KEYS)


def derive_from_observed_download(*, selector: str, affordance_text: str = "") -> ReachedDownloadTarget | None:
    """S3: mint a target from a download the scout's own click just produced.

    Stronger evidence than S2 (the download fired) while still carrying the selector S1 lacks, so the
    synthesizer can compile its terminal ``expect_download`` step against the exercised affordance."""
    selector = _summary_str(selector)
    if not selector:
        return None
    return ReachedDownloadTarget(
        selector=selector,
        affordance_text=_summary_str(affordance_text),
        download_kind=DOWNLOAD_KIND_OBSERVED,
        source_step=_SourceStepKind.observed_download.value,
        already_registered=False,
    )


def derive_from_observed_render(
    *, selector: str, rendered_url: str, affordance_text: str = ""
) -> ReachedDownloadTarget | None:
    """S3 sibling: mint a target from a document the scout's click opened as an inline render,
    so the guidance layer can steer the model away from the download idioms that cannot work on it."""
    selector = _summary_str(selector)
    rendered_url = _summary_str(rendered_url)
    if not selector or not rendered_url:
        return None
    return ReachedDownloadTarget(
        selector=selector,
        affordance_text=_summary_str(affordance_text),
        download_kind=DOWNLOAD_KIND_OBSERVED_RENDER,
        source_step=_SourceStepKind.observed_render.value,
        already_registered=False,
        rendered_url=rendered_url,
    )


def derive_from_block_outputs(
    block_outputs_by_label: Any, *, generated: frozenset[str]
) -> ReachedDownloadTarget | None:
    """S1: confirm a reached download from a browser download already registered into a block output.

    This is hard proof a download fired; the typed field carries no selector because the affordance
    has already been exercised — the agent only needs to know the run converged on a real download."""
    if not isinstance(block_outputs_by_label, dict):
        return None
    for label, output in block_outputs_by_label.items():
        if block_output_has_registered_download(output, generated):
            return ReachedDownloadTarget(
                selector="",
                affordance_text="",
                download_kind=DOWNLOAD_KIND_REGISTERED,
                source_step=_summary_str(label) or _SourceStepKind.registered_output.value,
                already_registered=True,
            )
    return None


def can_deliver_registered_download(target: ReachedDownloadTarget | None) -> bool:
    """Whether this target can still become a registered download in the generated workflow.

    An ``observed_render`` affordance compiles no terminal (registering its bytes needs the
    execution layer), so it must never credit a download deliverable as reachable or reached.
    """
    if target is None:
        return False
    return target.download_kind != DOWNLOAD_KIND_OBSERVED_RENDER


_EXPECT_DOWNLOAD_ATTR = "expect_download"
_DOWNLOAD_EVENT_CAPTURE_ATTRS: frozenset[str] = frozenset(
    {"wait_for_event", "expect_event", "on", "once", "add_listener"}
)
_DOWNLOAD_EVENT_NAME = "download"
_REGISTERED_DOWNLOAD_OUTPUT_KEY_SET = frozenset(REGISTERED_DOWNLOAD_OUTPUT_KEYS)


# Sandbox helper that clicks a download affordance and claims the fired browser download into the
# run download directory — the fired-download terminal shape both engines execute for one known affordance.
DOWNLOAD_CLAIM_HELPER_NAME = "click_and_claim_download"


@dataclass(frozen=True, slots=True)
class DownloadClaimHelperContract:
    """Point-of-use CodeBlock runtime contract for the fired-download helper."""

    name: str = DOWNLOAD_CLAIM_HELPER_NAME
    call: str = f"await {DOWNLOAD_CLAIM_HELPER_NAME}(page, selector)"
    page_type: str = "current_code_block_page"
    # The secure runner also accepts its locator facade, but the inline engine accepts strings.
    # This model-facing contract advertises only the shape executable by both engines.
    selector_types: tuple[str, ...] = ("selector_string",)
    return_type: str = "string"
    return_value: str = "sanitized_suggested_filename"

    def to_tool_data(self) -> dict[str, Any]:
        return {
            "call": self.call,
            "parameters": {
                "page": {"accepted_type": self.page_type},
                "selector": {"accepted_types": list(self.selector_types)},
            },
            "returns": {"type": self.return_type, "value": self.return_value},
        }


_DOWNLOAD_CLAIM_HELPER_CONTRACT = DownloadClaimHelperContract()


def download_claim_helper_contract() -> dict[str, dict[str, Any]]:
    """Return the model-facing helper map rendered by the code-block schema surface."""
    contract = _DOWNLOAD_CLAIM_HELPER_CONTRACT
    return {contract.name: contract.to_tool_data()}


def _call_is_expect_download(node: ast.expr) -> bool:
    if isinstance(node, ast.Await):
        return _call_is_expect_download(node.value)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return node.func.attr == _EXPECT_DOWNLOAD_ATTR
    return False


def _call_is_download_event_capture(node: ast.expr) -> bool:
    """True for the event-based download idioms -- ``wait_for_event``/``expect_event`` and the
    listener spellings ``on``/``once``/``add_listener`` -- taking the literal ``"download"`` as their
    first argument (await-unwrapped).

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


def code_uses_download_claim(code: str) -> bool:
    """True when the code calls the worker-owned download claim, the registering terminal for a
    fired-download affordance on both engines."""
    if not code.strip():
        return False
    try:
        tree = ast.parse(textwrap.dedent(code).strip() or "pass")
    except SyntaxError:
        return False
    return any(_call_is_download_claim(node) for node in ast.walk(tree) if isinstance(node, (ast.Call, ast.Await)))


def _call_is_download_claim(node: ast.expr) -> bool:
    if isinstance(node, ast.Await):
        return _call_is_download_claim(node.value)
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == DOWNLOAD_CLAIM_HELPER_NAME


def code_is_download_intent(code: str) -> bool:
    """True when a code block authors a download: it uses the `page.expect_download` context-manager
    idiom, any event-based `"download"` capture (`wait_for_event`/`expect_event`/`on`/`once`/
    `add_listener`), or the `click_and_claim_download` helper anywhere, or returns/binds a dict
    literal carrying an execution-layer download registration key. It stays deliberately broad —
    every download idiom counts, whether or not the runner accepts it — because
    its consumer is the unregistered-download telemetry in workflow/models/block.py, which measures
    authorship rather than validity."""
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
        if isinstance(node, (ast.Call, ast.Await)) and (
            _call_is_download_event_capture(node) or _call_is_download_claim(node)
        ):
            return True
        if isinstance(node, ast.Return) and node.value is not None:
            if _dict_literal_keys(node.value) & _REGISTERED_DOWNLOAD_OUTPUT_KEY_SET:
                return True
        if isinstance(node, ast.Assign):
            if _dict_literal_keys(node.value) & _REGISTERED_DOWNLOAD_OUTPUT_KEY_SET:
                return True
    return False
