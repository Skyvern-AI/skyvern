"""Deterministic synthesis of a copilot `code` block from a scout trajectory.

Pure module: ``synthesize_code_block`` is a function of its input trajectory
only — no LLM, no I/O, byte-identical output per trajectory. It turns the
scout's captured interaction sequence into a bounded, linear Playwright snippet
that runs on the raw ``page`` object the copilot code block executes against.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import keyword
import re
import textwrap
import tokenize
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Any, NamedTuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import structlog

from skyvern.forge.sdk.copilot.authoring_parameter_binding import (
    AuthoringParameterBindingSnapshot,
    authoring_parameter_binding_fingerprint,
)
from skyvern.forge.sdk.copilot.composition_evidence import SCOUT_INTERACTION_EVIDENCE_TOOL
from skyvern.forge.sdk.copilot.output_extraction_plan import (
    FrozenRequestedOutputExtractionCandidate,
    LiveReadBinding,
    LiveReadKind,
    RequestedOutputExtractionPlan,
    output_path_segments,
)
from skyvern.forge.sdk.copilot.reached_download_target import ReachedDownloadTarget
from skyvern.forge.sdk.copilot.runtime import ScoutedFieldParameterBinding, ScoutedInputCorrespondence
from skyvern.utils.strings import escape_code_fences

LOG = structlog.get_logger()

_MAX_STEPS = 60
_INDENT = "    "
_DOMCONTENTLOADED = "domcontentloaded"
_ENTRY_TARGET_VAR = "_scout_entry_target"
_ENTRY_REUSED_VAR = "_scout_entry_reused_current_page"
_ENTRY_RESUME_AFTER_AUTH_VAR = "_scout_entry_resume_after_auth"
_ENTRY_RESUME_TARGET_VAR = "_scout_entry_resume_target"
_ENTRY_OPENER_VAR = "_scout_entry_opener"
_OPTIONAL_DISMISSAL_VAR = "_scout_optional_dismissal"
_READONLY_DEFERRED_VAR = "_scout_readonly_actual"
_MONTH_HELPER_VAR = "_scout_month_to_iso"
_ENTRY_LOCATOR_VARS = (_ENTRY_TARGET_VAR, _ENTRY_RESUME_TARGET_VAR, _ENTRY_OPENER_VAR)
_INTERNAL_SCOUT_VARS = (
    _ENTRY_TARGET_VAR,
    _ENTRY_REUSED_VAR,
    _ENTRY_RESUME_AFTER_AUTH_VAR,
    _ENTRY_RESUME_TARGET_VAR,
    _ENTRY_OPENER_VAR,
    _OPTIONAL_DISMISSAL_VAR,
    _READONLY_DEFERRED_VAR,
    _MONTH_HELPER_VAR,
)

# Base name for the download var bound by `async with page.expect_download() as <name>:`.
_DOWNLOAD_VAR_BASE = "dl_info"
_DOWNLOAD_FILENAME_VAR_BASE = "downloaded_file_name"
_DOWNLOAD_PATH_VAR_BASE = "_downloaded_file_path"
_DOWNLOAD_OUTPUT_VAR_BASE = "downloaded_files"

CREDENTIAL_FILL_TOOL_NAME = "fill_credential_field"
_CREDENTIAL_FIELDS = frozenset({"username", "password", "totp"})

# Shape of a synthesized credential fill, ``.fill(<param>.<field>)`` or the runtime OTP
# accessor ``.fill(await <param>.otp())`` — distinguishes a login fill from a plain
# ``.fill(str(<key>))`` text input.
CREDENTIAL_FILL_CODE_PATTERN = re.compile(r"\.fill\(\s*(?:[A-Za-z_]\w*\.\w+|await\s+[A-Za-z_]\w*\.otp\(\))\s*\)")
# Credential fields the scout must fill live before a code block reading them may persist;
# `.otp()` resolves at runtime only, so totp never requires (or credits) a live scout fill.
LIVE_SCOUT_CREDENTIAL_FIELDS = frozenset({"username", "password"})
_CREDENTIAL_FIELD_ACCESS_RE = re.compile(
    r"\b(?P<parameter>[A-Za-z_][A-Za-z0-9_]*)\.(?:(?P<field>username|password|totp)\b|(?P<otp_method>otp)\s*\()"
)
_CODE_SUBMIT_ACTION_RE = re.compile(r"\.(?:click|press)\s*\(")


def _is_submit_interaction(interaction: Mapping[str, Any]) -> bool:
    """A submit is a click, or an Enter keypress; other keys (Tab between fields) are not submits, so
    both the synthesis submit boundary and the persist-time credential-scout gate share one definition."""
    tool_name = str(interaction.get("tool_name") or "").strip()
    if tool_name == "click":
        return True
    return tool_name == "press_key" and str(interaction.get("key") or "").strip() == "Enter"


class CredentialFieldAccess(NamedTuple):
    parameter_key: str
    field: str
    requires_live_scout: bool


def _credential_field_accesses(code: str) -> list[CredentialFieldAccess]:
    accesses: list[CredentialFieldAccess] = []
    for match in _CREDENTIAL_FIELD_ACCESS_RE.finditer(code):
        field = match.group("field")
        if field:
            accesses.append(
                CredentialFieldAccess(
                    parameter_key=match.group("parameter"),
                    field=field,
                    requires_live_scout=True,
                )
            )
            continue
        if match.group("otp_method"):
            accesses.append(
                CredentialFieldAccess(
                    parameter_key=match.group("parameter"),
                    field="totp",
                    requires_live_scout=False,
                )
            )
    return accesses


class ScoutGap(NamedTuple):
    missing_fields: list[str]
    missing_submit: bool


def first_matched_post_fill_submit_index(
    trajectory: Sequence[Mapping[str, Any]],
    latest_fill_index: int,
    matched_source_urls: AbstractSet[str],
) -> int | None:
    for index, interaction in enumerate(trajectory):
        if index <= latest_fill_index:
            continue
        if not _is_submit_interaction(interaction):
            continue
        source_url = str(interaction.get("source_url") or "").strip()
        if matched_source_urls and source_url not in matched_source_urls:
            continue
        return index
    return None


def credential_scout_gap(
    trajectory: Sequence[Mapping[str, Any]],
    requirements: Sequence[tuple[AbstractSet[str], AbstractSet[str]]],
    *,
    requires_submit: bool,
) -> ScoutGap:
    """Match one block's credential requirements — (allowed_credential_ids, required_fields) tuples —
    against the scout trajectory: fill indexes and source urls accumulate across requirement tuples, and
    a single post-latest-fill submit on a matched source url satisfies ``requires_submit`` globally."""
    matched_fill_indexes: list[int] = []
    matched_source_urls: set[str] = set()
    missing_fields: list[str] = []
    for allowed_credential_ids, required_fields in requirements:
        matched_fields: set[str] = set()
        for index, interaction in enumerate(trajectory):
            if str(interaction.get("tool_name") or "").strip() != CREDENTIAL_FILL_TOOL_NAME:
                continue
            if str(interaction.get("credential_id") or "").strip() not in allowed_credential_ids:
                continue
            field = str(interaction.get("credential_field") or "").strip()
            if field not in required_fields:
                continue
            matched_fields.add(field)
            matched_fill_indexes.append(index)
            source_url = str(interaction.get("source_url") or "").strip()
            if source_url:
                matched_source_urls.add(source_url)
        for field in sorted(required_fields - matched_fields):
            missing_fields.append(field)

    missing_submit = False
    if requires_submit:
        latest_fill_index = max(matched_fill_indexes, default=-1)
        missing_submit = (
            latest_fill_index < 0
            or first_matched_post_fill_submit_index(trajectory, latest_fill_index, matched_source_urls) is None
        )
    return ScoutGap(missing_fields=missing_fields, missing_submit=missing_submit)


_ENTRY_TARGET_TOOLS = frozenset({"click", "type_text", CREDENTIAL_FILL_TOOL_NAME, "select_option", "press_key"})
_DURABLE_FALLBACK_ENTRY_TARGET_TOOLS = frozenset({"type_text", CREDENTIAL_FILL_TOOL_NAME, "select_option"})
_OPTIONAL_DISMISSAL_NAME_PATTERN = re.compile(
    r"\b(?:accept|agree|allow|consent|cookies?|decline|reject|refuse|dismiss|got it|no thanks)\b|^(?:ok|okay)$",
    re.I,
)
_OPTIONAL_DISMISSAL_SELECTOR_PATTERN = re.compile(
    r"(?:acceptcookies|cookies?|consent|decline|reject|refuse|dismiss|close)", re.I
)
# Used only after the scout captured an unnamed structural/not-decline cookie click
# and a later durable target exists; this generic text fallback keeps replay conditional.
_COOKIE_ACCEPT_FALLBACK_LOCATOR_SELECTOR = "button:has-text('Accept')"
_NOT_DECLINE_BUTTON_SELECTOR_PATTERN = re.compile(r"^button:not\(\.decline\)(?::nth-of-type\(\d+\))?$", re.I)
_COOKIE_ACCEPT_TEXT_XPATH_PATTERN = re.compile(
    r"""^//button\[\s*normalize-space\(\)\s*=\s*(['"])accept\1\s*\]$""", re.I
)
_BODY_ROOTED_INDEXED_BUTTON_XPATH_PATTERN = re.compile(
    r"""^(?:/\*\[name\(\)=["']html["']\]\[1\])?"""
    r"""(?:/\*\[name\(\)=["']body["']\]\[1\])"""
    r"""(?:/\*\[name\(\)=["'][a-z0-9_-]+["']\]\[\d+\])*"""
    r"""/\*\[name\(\)=["']button["']\]\[\d+\]$""",
    re.I,
)
_STRUCTURAL_DISMISSAL_SELECTOR_PATTERN = re.compile(
    r"^(?:[.#][A-Za-z_][\w-]*\s+)?button(?::nth-(?:of-type|child)\(\d+\))$"
    r"|^button:not\(\.decline\)(?::nth-of-type\(\d+\))?$",
    re.I,
)

_SYNTHESIZED_BLOCK_LABEL = "scout_synthesized_browser_steps"

# Names the code-block executor reserves in its exec() namespace (block.py build_safe_vars
# plus the injected `page`). A parameter key colliding with one of these is silently dropped
# at bind time, so the synthesized fill would stringify the builtin instead of the user value.
# "username"/"password"/"totp"/"totp_identifier" are reserved too: CodeBlock.execute also
# injects a bound credential's fields under those bare names, so a plain parameter named
# `password` would resolve to the credential's secret value instead of the user input.
_RESERVED_PARAM_NAMES = frozenset(
    {
        "page",
        "username",
        "password",
        "totp",
        "totp_identifier",
        "otp",
        "print",
        "len",
        "range",
        "str",
        "int",
        "float",
        "dict",
        "list",
        "tuple",
        "set",
        "bool",
        "isinstance",
        "enumerate",
        "any",
        "all",
        "max",
        "min",
        "sum",
        "sorted",
        "sleep",
        "asyncio",
        "re",
        "json",
        "html",
        "Exception",
        _ENTRY_TARGET_VAR,
        _ENTRY_REUSED_VAR,
        _ENTRY_RESUME_AFTER_AUTH_VAR,
        _ENTRY_RESUME_TARGET_VAR,
        _ENTRY_OPENER_VAR,
        _MONTH_HELPER_VAR,
        _DOWNLOAD_VAR_BASE,
        f"{_DOWNLOAD_VAR_BASE}_file",
        _DOWNLOAD_FILENAME_VAR_BASE,
        _DOWNLOAD_PATH_VAR_BASE,
        _DOWNLOAD_OUTPUT_VAR_BASE,
    }
)

# role=<role>[name="<name>"] optionally followed by `>> nth=<n>` or other engines.
_ROLE_NAME_RE = re.compile(r'^role=([a-zA-Z]+)(?:\[name="((?:[^"\\]|\\.)*)"\])?(.*)$')

# Positional/index engines whose match depends on document order, not element identity. A captured
# selector containing one of these is fragile, so an ARIA role/name anchor (when available) is preferred.
_POSITIONAL_RE = re.compile(
    r":nth-of-type\(|:nth-child\(|:nth-last-of-type\(|:nth-last-child\(|>>\s*nth=|:first-child|:last-child"
)

# A lone tag/role token (`button`, `a`) matches every such element, so a bare emission is not
# unique under Playwright strict mode.
_BARE_TAG_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9-]*$")


@dataclass
class SynthesisDiagnostics:
    emitted_interaction_count: int = 0
    truncated: bool = False
    dropped_interactions: list[dict[str, Any]] = field(default_factory=list)
    # Emission ground truth recorded at each emission branch; with dropped/forgiven it partitions the
    # retained trajectory indices. Diagnostics-only, never serialized.
    emitted_interactions: list[dict[str, Any]] = field(default_factory=list)
    forgiven_interactions: list[dict[str, Any]] = field(default_factory=list)
    download_terminal_anchor: int | None = None
    download_terminal_dropped_trailing: int = 0
    # Post-download-cut trajectory indices recorded before the emission loop, so the partition obligation
    # can detect a truncation-break index that lands in no record lane instead of silently losing it.
    retained_trajectory_indices: list[int] = field(default_factory=list)
    locator_provenance: list[dict[str, Any]] = field(default_factory=list)
    # (trajectory enumerate index -> minted type_text parameter key); diagnostics-only, never serialized.
    # Recovers the key for a typed field whose value was withheld from default_value (typed_value == "").
    typed_param_bindings: list[tuple[int, str]] = field(default_factory=list)
    grounded_submit_binding_fingerprints: list[str] = field(default_factory=list)


@dataclass
class SynthesizedCodeBlock:
    code: str
    parameters: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    diagnostics: SynthesisDiagnostics = field(default_factory=SynthesisDiagnostics)
    steps: list[dict[str, Any]] = field(default_factory=list)
    interaction_code: str = ""
    extraction_code: str = ""
    extraction_fingerprint: str = ""
    extraction_plan_identity: str = ""


def grounded_parameter_key_is_safe(parameter_key: str) -> bool:
    return (
        parameter_key.isidentifier()
        and not keyword.iskeyword(parameter_key)
        and not parameter_key.startswith("__")
        and parameter_key not in _RESERVED_PARAM_NAMES
    )


def grounded_submit_rung_binding_fingerprint(
    *,
    repeated_structural_key: str,
    source_url: str,
    submit_selector: str,
    submit_trajectory_index: int,
    field_bindings: Sequence[ScoutedFieldParameterBinding],
) -> str:
    payload = {
        "repeated_structural_key": repeated_structural_key,
        "source_url": source_url,
        "submit_selector": submit_selector,
        "submit_trajectory_index": submit_trajectory_index,
        "field_bindings": [
            {
                "parameter_key": binding["parameter_key"],
                "field_selector": binding["field_selector"],
            }
            for binding in field_bindings
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _binding_source_origin(source_url: str) -> str:
    parsed = urlsplit(source_url)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""


def _captured_trajectory_index(interaction: Mapping[str, Any], position: int) -> int:
    raw_index = interaction.get("trajectory_index")
    return raw_index if isinstance(raw_index, int) and raw_index >= 0 else position


class _ValidatedSnapshotBindings(NamedTuple):
    fill_by_index: dict[int, tuple[str, str]]
    select_option_by_index: dict[int, str]


def _validated_authoring_parameter_binding_snapshot(
    snapshot: AuthoringParameterBindingSnapshot,
    trajectory: Sequence[Mapping[str, Any]],
) -> _ValidatedSnapshotBindings | None:
    if not snapshot.field_bindings:
        return None
    terminal_matches = [
        (position, interaction)
        for position, interaction in enumerate(trajectory)
        if _captured_trajectory_index(interaction, position) == snapshot.terminal.trajectory_index
    ]
    if len(terminal_matches) != 1:
        return None
    _terminal_position, terminal = terminal_matches[0]
    if _binding_source_origin(str(terminal.get("source_url") or "")) != snapshot.source_origin:
        return None
    if str(terminal.get("tool_name") or "") != snapshot.terminal.tool_name:
        return None
    if str(terminal.get("selector") or "").strip() != snapshot.terminal.selector:
        return None
    if str(terminal.get("key") or "").strip() != snapshot.terminal.key:
        return None
    expected = authoring_parameter_binding_fingerprint(
        structural_key=snapshot.structural_key,
        source_origin=snapshot.source_origin,
        field_bindings=snapshot.field_bindings,
        terminal=snapshot.terminal,
    )
    if expected != snapshot.fingerprint:
        return None
    fill_by_index: dict[int, tuple[str, str]] = {}
    select_option_by_index: dict[int, str] = {}
    declared_keys: set[str] = set()
    selectors: set[str] = set()
    for binding in snapshot.field_bindings:
        if (
            not grounded_parameter_key_is_safe(binding.declared_key)
            or not binding.field_selector
            or binding.declared_key in declared_keys
            or binding.field_selector in selectors
        ):
            return None
        declared_keys.add(binding.declared_key)
        selectors.add(binding.field_selector)
        if binding.field_trajectory_index is None:
            continue
        field_matches = [
            (position, interaction)
            for position, interaction in enumerate(trajectory)
            if _captured_trajectory_index(interaction, position) == binding.field_trajectory_index
        ]
        if len(field_matches) != 1:
            return None
        field_position, interaction = field_matches[0]
        if _binding_source_origin(str(interaction.get("source_url") or "")) != snapshot.source_origin:
            return None
        if binding.match_basis == "scouted_selection_value":
            if str(interaction.get("tool_name") or "") != "click":
                return None
            if templated_selection_locator_binding(interaction) != (binding.declared_key, binding.field_selector):
                return None
            continue
        if binding.match_basis == "scouted_option_value":
            if str(interaction.get("tool_name") or "") != "select_option":
                return None
            if str(interaction.get("selector") or "").strip() != binding.field_selector:
                return None
            if not selection_option_value_admissible(str(interaction.get("value") or "").strip(), binding.declared_key):
                return None
            select_option_by_index[field_position] = binding.declared_key
            continue
        if str(interaction.get("tool_name") or "") != "type_text":
            return None
        if str(interaction.get("selector") or "").strip() != binding.field_selector:
            return None
        fill_by_index[field_position] = (binding.declared_key, binding.field_selector)
    return _ValidatedSnapshotBindings(fill_by_index, select_option_by_index)


def _validated_submit_rung_binding(
    interaction: Mapping[str, Any], trajectory_index: int
) -> tuple[str, tuple[tuple[str, str], ...]] | None:
    raw = interaction.get("submit_rung_binding")
    if not isinstance(raw, Mapping) or str(interaction.get("tool_name") or "") != "click":
        return None
    repeated_structural_key = str(raw.get("repeated_structural_key") or "").strip()
    fingerprint = str(raw.get("fingerprint") or "").strip()
    raw_fields = raw.get("field_bindings")
    if not repeated_structural_key or not fingerprint or not isinstance(raw_fields, list) or not raw_fields:
        return None
    fields: list[ScoutedFieldParameterBinding] = []
    parameter_keys: set[str] = set()
    field_selectors: set[str] = set()
    for raw_field in raw_fields:
        if not isinstance(raw_field, Mapping):
            return None
        parameter_key = str(raw_field.get("parameter_key") or "").strip()
        field_selector = str(raw_field.get("field_selector") or "").strip()
        if (
            not grounded_parameter_key_is_safe(parameter_key)
            or not field_selector
            or parameter_key in parameter_keys
            or field_selector in field_selectors
        ):
            return None
        parameter_keys.add(parameter_key)
        field_selectors.add(field_selector)
        fields.append({"parameter_key": parameter_key, "field_selector": field_selector})
    if [field["parameter_key"] for field in fields] != sorted(parameter_keys):
        return None
    submit_selector = str(interaction.get("selector") or "").strip()
    source_url = str(interaction.get("source_url") or "").strip()
    if not submit_selector or not source_url:
        return None
    expected = grounded_submit_rung_binding_fingerprint(
        repeated_structural_key=repeated_structural_key,
        source_url=source_url,
        submit_selector=submit_selector,
        submit_trajectory_index=trajectory_index,
        field_bindings=fields,
    )
    if fingerprint != expected:
        return None
    return fingerprint, tuple((field["parameter_key"], field["field_selector"]) for field in fields)


@dataclass(frozen=True, slots=True)
class SynthesizedExtractionSuffix:
    code: str
    fingerprint: str


@dataclass
class _ExtractionReturnNode:
    children: dict[str, _ExtractionReturnNode] = field(default_factory=dict)
    value_expression: str = ""


# str.splitlines() and several parsers treat these codepoints as line boundaries, so a raw one in a
# captured accessible name or option value would split the emitted one-line literal across lines and
# corrupt the block. repr() does not escape U+2028/U+2029, so they (and the C0/C1 controls below) are
# escaped explicitly to keep every emitted literal single-line.
_EXTRA_LINE_SEPARATORS = ("\u2028", "\u2029")
_CONTROL_CODEPOINTS = frozenset(
    chr(cp) for cp in (*range(0x00, 0x20), 0x7F, *range(0x80, 0xA0)) if chr(cp) not in ("\n", "\r")
)
_SENSITIVE_URL_QUERY_RE = re.compile(
    r"(?:password|passwd|token|secret|api[_-]?key|credential|auth|session|cookie)", re.I
)


def _scrub_url_for_code_literal(url: str) -> str:
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return url
    if not parts.scheme or not parts.netloc or hostname is None:
        return url

    netloc = hostname
    if ":" in netloc and not netloc.startswith("["):
        netloc = f"[{netloc}]"
    if port is not None:
        netloc = f"{netloc}:{port}"

    query = urlencode(
        [
            (key, "__redacted__" if _SENSITIVE_URL_QUERY_RE.search(key) else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ],
        doseq=True,
    )
    fragment = parts.fragment
    fragment_pairs = parse_qsl(fragment, keep_blank_values=True) if "=" in fragment or "&" in fragment else []
    if fragment_pairs:
        fragment = urlencode(
            [(key, "__redacted__" if _SENSITIVE_URL_QUERY_RE.search(key) else value) for key, value in fragment_pairs],
            doseq=True,
        )
    elif fragment and _SENSITIVE_URL_QUERY_RE.search(fragment):
        fragment = "__redacted__"
    return urlunsplit((parts.scheme, netloc, parts.path, query, fragment))


def _py_str(value: str) -> str:
    """A deterministic double-quoted Python string literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    escaped = "".join(f"\\x{ord(ch):02x}" if ch in _CONTROL_CODEPOINTS else ch for ch in escaped)
    for separator in _EXTRA_LINE_SEPARATORS:
        escaped = escaped.replace(separator, f"\\u{ord(separator):04x}")
    return f'"{escaped}"'


def _unescape_role_name(value: str) -> str:
    # ref_to_selector escapes only `"` as `\"`; reverse it for the get_by_role name.
    return value.replace('\\"', '"')


def _parse_role_name(selector: str) -> tuple[str, str | None, str] | None:
    """Parse a `role=...[name="..."]` selector. Returns (role, name, suffix) or None.

    ``suffix`` is the residual engine chain (e.g. ` >> nth=2`); a non-empty suffix
    means the selector cannot be expressed as a plain get_by_role and the caller
    falls back to page.locator.
    """
    match = _ROLE_NAME_RE.match(selector)
    if not match:
        return None
    role, raw_name, suffix = match.group(1), match.group(2), match.group(3)
    name = _unescape_role_name(raw_name) if raw_name is not None else None
    return role, name, suffix.strip()


def _is_positional_selector(selector: str) -> bool:
    """True when the captured selector's match depends on document position, not element identity.

    Stable anchors (id, [name=...], [data-testid=...], [aria-label=...], a non-indexed CSS path) are
    preferred verbatim; only a positional/index selector is worth trading for an ARIA role/name anchor.
    """
    return bool(_POSITIONAL_RE.search(selector))


def _is_bare_ambiguous_selector(selector: str) -> bool:
    """True when the captured selector is a lone tag/role token or the universal `*` with no qualifier."""
    stripped = selector.strip()
    return stripped == "*" or bool(_BARE_TAG_RE.match(stripped))


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().lower()).strip("_")
    if cleaned and cleaned[0].isdigit():
        cleaned = f"field_{cleaned}"
    return cleaned or "field"


def _safe_param_base(value: str) -> str:
    base = _slug(value)
    if keyword.iskeyword(base) or base in _RESERVED_PARAM_NAMES:
        return f"{base}_field"
    return base


def _get_by_role_expr(role: str, name: str) -> str:
    if name:
        return f"page.get_by_role({_py_str(role)}, name={_py_str(name)})"
    # A nameless role matches every element of that role; `.first` pins the scout's document-order pick.
    return f"page.get_by_role({_py_str(role)}).first"


def _get_by_role_expr_strict(role: str, name: str) -> str:
    """Strict re-anchor: exact name match so a repeated affordance resolves to a single (role, name)
    element where the substring default over-matches. N identical exact names still strict-mode-violate
    at run time (SKY-11297) — an honest failure beats a silent wrong-element click."""
    return f"page.get_by_role({_py_str(role)}, name={_py_str(name)}, exact=True)"


LOCATOR_WITNESS_PARAM_SOURCE = "locator_witness"
INPUT_TEMPLATED_PROVENANCE_SOURCE = "input_templated"
_SCOUT_MONTH_HELPER_NAME = _MONTH_HELPER_VAR
_WITNESS_MIN_VALUE_LEN = 3
_WITNESS_SAFE_CHARSET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
_WITNESS_KEY_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WITNESS_MONTH_TO_ISO = {
    "january": "01",
    "february": "02",
    "march": "03",
    "april": "04",
    "may": "05",
    "june": "06",
    "july": "07",
    "august": "08",
    "september": "09",
    "october": "10",
    "november": "11",
    "december": "12",
}


class _InputTemplatingPlan(NamedTuple):
    surface: str
    selector: str
    role: str
    name: str
    holes: list[Mapping[str, Any]]


def _witness_key_is_safe(key: str) -> bool:
    if not _WITNESS_KEY_IDENT_RE.fullmatch(key):
        return False
    if keyword.iskeyword(key):
        return False
    if key.startswith("_scout"):
        return False
    return key not in _RESERVED_PARAM_NAMES


def _month_name_to_iso(value: str) -> str | None:
    parts = value.split()
    if len(parts) != 2:
        return None
    month = _WITNESS_MONTH_TO_ISO.get(parts[0].lower())
    year = parts[1]
    if month is None or len(year) != 4 or not year.isdigit():
        return None
    return f"{year}-{month}"


def _witness_observed_forms(value: str) -> list[tuple[str, str]]:
    forms: list[tuple[str, str]] = [("identity", value)]
    iso = _month_name_to_iso(value)
    if iso is not None and iso != value:
        forms.append(("month_name_to_iso", iso))
    return forms


def _quoted_content_spans(selector: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    quote = ""
    start = -1
    i = 0
    length = len(selector)
    while i < length:
        ch = selector[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                spans.append((start, i))
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
            start = i + 1
        i += 1
    return spans


def _boundary_delimited_positions(haystack: str, needle: str, allowed_spans: Sequence[tuple[int, int]]) -> list[int]:
    positions: list[int] = []
    if not needle:
        return positions
    start = 0
    while True:
        idx = haystack.find(needle, start)
        if idx < 0:
            break
        end = idx + len(needle)
        left_ok = idx == 0 or not haystack[idx - 1].isalnum()
        right_ok = end == len(haystack) or not haystack[end].isalnum()
        inside = any(span_start <= idx and end <= span_end for span_start, span_end in allowed_spans)
        if left_ok and right_ok and inside:
            positions.append(idx)
        start = idx + 1
    return positions


def _resolve_non_competing_correspondences(raw: list[dict[str, Any]]) -> list[ScoutedInputCorrespondence]:
    result: list[ScoutedInputCorrespondence] = []
    for surface in ("selector", "accessible_name"):
        entries = sorted((entry for entry in raw if entry["surface"] == surface), key=lambda entry: entry["_position"])
        key_counts: dict[str, int] = {}
        for entry in entries:
            key_counts[entry["input_key"]] = key_counts.get(entry["input_key"], 0) + 1
        bad: set[int] = set()
        for a_index, a_entry in enumerate(entries):
            if key_counts[a_entry["input_key"]] > 1:
                bad.add(a_index)
            a_start = a_entry["_position"]
            a_end = a_start + len(a_entry["matched_literal"])
            for b_index in range(a_index + 1, len(entries)):
                b_start = entries[b_index]["_position"]
                b_end = b_start + len(entries[b_index]["matched_literal"])
                if a_start < b_end and b_start < a_end:
                    bad.add(a_index)
                    bad.add(b_index)
        for index, entry in enumerate(entries):
            if index in bad:
                continue
            result.append(
                {
                    "input_key": entry["input_key"],
                    "matched_literal": entry["matched_literal"],
                    "parameter_value": entry["parameter_value"],
                    "surface": entry["surface"],
                    "transform": entry["transform"],
                    "position": entry["_position"],
                }
            )
    return result


def input_correspondences_for_interaction(
    interaction: Mapping[str, Any], declared_params: Mapping[str, str]
) -> list[ScoutedInputCorrespondence]:
    """Witness a declared parameter value observed verbatim (identity, or month-name -> ISO) inside a
    quoted selector segment or the accessible name at click time — value containment, never label==header
    matching. Empty unless the match is unique across both surfaces, boundary-delimited, safe-charset on
    value and literal, whitespace-normalized, and name-safe."""
    if str(interaction.get("tool_name") or "") != "click":
        return []
    selector = str(interaction.get("selector") or "").strip()
    name = str(interaction.get("accessible_name") or "").strip()
    selector_spans = _quoted_content_spans(selector)
    name_spans = [(0, len(name))] if name else []
    raw: list[dict[str, Any]] = []
    for key in sorted(declared_params):
        value = declared_params[key]
        if not value or value != value.strip() or len(value) < _WITNESS_MIN_VALUE_LEN:
            continue
        if not _WITNESS_SAFE_CHARSET_RE.fullmatch(value):
            continue
        if not _witness_key_is_safe(key):
            continue
        for transform, observed in _witness_observed_forms(value):
            if len(observed) < _WITNESS_MIN_VALUE_LEN or not _WITNESS_SAFE_CHARSET_RE.fullmatch(observed):
                continue
            selector_positions = _boundary_delimited_positions(selector, observed, selector_spans)
            name_positions = _boundary_delimited_positions(name, observed, name_spans)
            if len(selector_positions) + len(name_positions) != 1:
                continue
            if selector_positions:
                surface, position = "selector", selector_positions[0]
            else:
                surface, position = "accessible_name", name_positions[0]
            raw.append(
                {
                    "surface": surface,
                    "input_key": key,
                    "matched_literal": observed,
                    "parameter_value": value,
                    "transform": transform,
                    "_position": position,
                }
            )
    return _resolve_non_competing_correspondences(raw)


def _escape_fstring_literal_segment(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    escaped = "".join(f"\\x{ord(ch):02x}" if ch in _CONTROL_CODEPOINTS else ch for ch in escaped)
    for separator in _EXTRA_LINE_SEPARATORS:
        escaped = escaped.replace(separator, f"\\u{ord(separator):04x}")
    return escaped.replace("{", "{{").replace("}", "}}")


def _interpolate_holes(raw: str, holes: Sequence[Mapping[str, Any]]) -> str | None:
    segments: list[str] = []
    cursor = 0
    for hole in holes:
        matched_literal = str(hole.get("matched_literal") or "")
        idx = hole.get("position")
        # Interpolate at the boundary-validated span carried from the witness, not a naive substring
        # scan: a value that also occurs earlier as a non-boundary substring would template the wrong span.
        if not isinstance(idx, int) or idx < cursor or raw[idx : idx + len(matched_literal)] != matched_literal:
            return None
        segments.append(_escape_fstring_literal_segment(raw[cursor:idx]))
        key = str(hole.get("input_key") or "")
        if str(hole.get("transform") or "identity") == "month_name_to_iso":
            segments.append("{" + _SCOUT_MONTH_HELPER_NAME + "(" + key + ")}")
        else:
            segments.append("{" + key + "}")
        cursor = idx + len(matched_literal)
    segments.append(_escape_fstring_literal_segment(raw[cursor:]))
    return "".join(segments)


def build_input_templated_locator(
    *, surface: str, selector: str, role: str, name: str, holes: Sequence[Mapping[str, Any]]
) -> str | None:
    """Single source for the templated locator literal, used at emission AND re-derived byte-for-byte at
    the admissibility seam so a tampered or reordered provenance record fails the recompute equality check."""
    if not holes:
        return None
    if surface == "selector":
        body = _interpolate_holes(selector, holes)
        if body is None:
            return None
        return f'page.locator(f"{body}")'
    if surface == "accessible_name":
        if not role or not name:
            return None
        body = _interpolate_holes(name, holes)
        if body is None:
            return None
        return f'page.get_by_role({_py_str(role)}, name=f"{body}", exact=True)'
    return None


def templated_selection_locator_binding(interaction: Mapping[str, Any]) -> tuple[str, str] | None:
    """(declared_key, canonical templated-locator expression) for a click whose stamped
    input_correspondences template exactly one declared-key hole. None when the click is untemplatable
    or witnesses more than one hole. The canonical expression is the join key shared with the consumption
    recognizer, so a re-authored templated click and this snapshot binding agree by construction."""
    plan = _input_templating_plan(interaction)
    if plan is None or len(plan.holes) != 1:
        return None
    key = str(plan.holes[0].get("input_key") or "")
    if not key:
        return None
    expr = build_input_templated_locator(
        surface=plan.surface, selector=plan.selector, role=plan.role, name=plan.name, holes=plan.holes
    )
    if expr is None:
        return None
    try:
        canonical = ast.unparse(ast.parse(expr, mode="eval").body)
    except SyntaxError:
        return None
    return key, canonical


def selection_option_value_admissible(value: str, key: str) -> bool:
    return (
        value == value.strip()
        and len(value) >= _WITNESS_MIN_VALUE_LEN
        and bool(_WITNESS_SAFE_CHARSET_RE.fullmatch(value))
        and _witness_key_is_safe(key)
    )


def _ordered_holes(raw: str, holes: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]] | None:
    positioned: list[tuple[int, Mapping[str, Any]]] = []
    for hole in holes:
        matched_literal = str(hole.get("matched_literal") or "")
        idx = hole.get("position")
        if not isinstance(idx, int) or raw[idx : idx + len(matched_literal)] != matched_literal:
            return None
        positioned.append((idx, hole))
    positioned.sort(key=lambda item: item[0])
    return [hole for _, hole in positioned]


def _input_templating_plan(interaction: Mapping[str, Any]) -> _InputTemplatingPlan | None:
    correspondences = interaction.get("input_correspondences")
    if not isinstance(correspondences, list) or not correspondences:
        return None
    selector = str(interaction.get("selector") or "").strip()
    role = str(interaction.get("role") or "").strip()
    name = str(interaction.get("accessible_name") or "").strip()
    selector_holes = [c for c in correspondences if isinstance(c, Mapping) and c.get("surface") == "selector"]
    name_holes = [c for c in correspondences if isinstance(c, Mapping) and c.get("surface") == "accessible_name"]
    parsed = _parse_role_name(selector) if selector else None
    if (
        selector_holes
        and selector
        and parsed is None
        and not _is_positional_selector(selector)
        and not _is_bare_ambiguous_selector(selector)
    ):
        ordered = _ordered_holes(selector, selector_holes)
        if ordered is not None:
            return _InputTemplatingPlan(surface="selector", selector=selector, role="", name="", holes=ordered)
    if name_holes and role and name:
        ambiguous_role = parsed is not None and not parsed[1]
        if not selector or _is_bare_ambiguous_selector(selector) or ambiguous_role:
            ordered = _ordered_holes(name, name_holes)
            if ordered is not None:
                return _InputTemplatingPlan(surface="accessible_name", selector="", role=role, name=name, holes=ordered)
    return None


def _maybe_input_templated_locator(
    interaction: Mapping[str, Any],
    *,
    diagnostics: SynthesisDiagnostics | None,
    trajectory_index: int | None,
) -> str | None:
    plan = _input_templating_plan(interaction)
    if plan is None:
        return None
    expr = build_input_templated_locator(
        surface=plan.surface, selector=plan.selector, role=plan.role, name=plan.name, holes=plan.holes
    )
    if expr is None:
        return None
    if diagnostics is not None:
        record: dict[str, Any] = {
            "trajectory_index": trajectory_index if trajectory_index is not None else -1,
            "source": INPUT_TEMPLATED_PROVENANCE_SOURCE,
            "surface": plan.surface,
            "emitted_literal": expr,
            "holes": [
                {
                    "input_key": str(hole.get("input_key") or ""),
                    "matched_literal": str(hole.get("matched_literal") or ""),
                    "parameter_value": str(hole.get("parameter_value") or ""),
                    "transform": str(hole.get("transform") or "identity"),
                    "position": hole.get("position"),
                }
                for hole in plan.holes
            ],
        }
        if plan.surface == "selector":
            record["selector"] = plan.selector
        else:
            record["role"] = plan.role
            record["name"] = plan.name
        diagnostics.locator_provenance.append(record)
    return expr


def _prescan_input_templating(trajectory: Sequence[Mapping[str, Any]]) -> tuple[list[str], bool]:
    keys: list[str] = []
    needs_month = False
    for interaction in trajectory:
        plan = _input_templating_plan(interaction)
        if plan is None:
            continue
        for hole in plan.holes:
            key = str(hole.get("input_key") or "")
            if key and key not in keys:
                keys.append(key)
            if str(hole.get("transform") or "identity") == "month_name_to_iso":
                needs_month = True
    return keys, needs_month


def _scout_month_helper_lines() -> list[str]:
    month_map_literal = "{" + ", ".join(f'"{name}": "{code}"' for name, code in _WITNESS_MONTH_TO_ISO.items()) + "}"
    return [
        f"{_INDENT}def {_SCOUT_MONTH_HELPER_NAME}(_value):",
        f"{_INDENT * 2}_months = {month_map_literal}",
        f"{_INDENT * 2}_parts = str(_value).split()",
        f"{_INDENT * 2}if len(_parts) != 2 or _parts[0].lower() not in _months or not (len(_parts[1]) == 4 "
        f"and _parts[1].isdigit()):",
        f'{_INDENT * 3}raise Exception("unrecognized month value for grounded parameter")',
        f'{_INDENT * 2}return _parts[1] + "-" + _months[_parts[0].lower()]',
    ]


def _witness_charset_guard_lines(key: str) -> list[str]:
    return [
        f"{_INDENT}if not (isinstance({key}, str) and {key} == {key}.strip() and {key}[:1].isalnum() "
        f'and all(_c.isalnum() or _c in " ._-" for _c in {key})):',
        f"{_INDENT * 2}raise Exception({_py_str(f'invalid value for grounded parameter {key}')})",
    ]


def witness_prelude_lines(keys: Sequence[str], *, include_month_helper: bool) -> list[str]:
    """Top-of-body guards (fail closed before any interpolation) plus the reserved month helper def.
    Reinjected into every separated browser stage because each stage is an independent CodeBlock."""
    lines: list[str] = []
    if include_month_helper:
        lines.extend(_scout_month_helper_lines())
    for key in keys:
        lines.extend(_witness_charset_guard_lines(key))
    return lines


def _locator_expr(
    interaction: Mapping[str, Any],
    notes: list[str],
    *,
    diagnostics: SynthesisDiagnostics | None = None,
    trajectory_index: int | None = None,
    tool_name: str = "",
    strict_selectors: bool = False,
) -> str:
    """Selector-first: emit the scout's captured working selector verbatim, and only fall back to an
    ARIA get_by_role anchor when that selector is positional/index-based (and a role/name is available).

    The accessible name is read on the scout/MCP surface; a `code:` block runs on a raw Playwright page
    where get_by_role's native name computation may not reproduce it. The captured selector is the proven,
    timing-independent locator the scout actually drove, so it wins for any stable selector.
    """
    selector = str(interaction.get("selector") or "").strip()
    role = str(interaction.get("role") or "").strip()
    name = str(interaction.get("accessible_name") or "").strip()
    scout_ambiguous = bool(interaction.get("ambiguous"))

    templated = _maybe_input_templated_locator(interaction, diagnostics=diagnostics, trajectory_index=trajectory_index)
    if templated is not None:
        return templated

    if strict_selectors:
        if not selector:
            notes.append("dropped an interaction with no selector")
            if diagnostics is not None:
                diagnostics.dropped_interactions.append(
                    {
                        "trajectory_index": trajectory_index if trajectory_index is not None else -1,
                        "tool_name": tool_name,
                        "reason_code": "missing_selector",
                    }
                )
            return ""
        parsed_strict = _parse_role_name(selector)
        ambiguous_role = parsed_strict is not None and not parsed_strict[1]
        if ambiguous_role or scout_ambiguous or _is_bare_ambiguous_selector(selector):
            if role and name:
                expr = _get_by_role_expr_strict(role, name)
                if diagnostics is not None:
                    diagnostics.locator_provenance.append(
                        {
                            "trajectory_index": trajectory_index if trajectory_index is not None else -1,
                            "selector": selector,
                            "emitted_literal": expr,
                            "source": "aria_role_name",
                            "role": role,
                            "name": name,
                        }
                    )
                return expr
            notes.append(f"dropped an interaction with an ambiguous bare selector {selector!r}")
            if diagnostics is not None:
                diagnostics.dropped_interactions.append(
                    {
                        "trajectory_index": trajectory_index if trajectory_index is not None else -1,
                        "tool_name": tool_name,
                        "selector": selector,
                        "reason_code": "ambiguous_bare_selector",
                    }
                )
            return ""
        if diagnostics is not None:
            diagnostics.locator_provenance.append(
                {
                    "trajectory_index": trajectory_index if trajectory_index is not None else -1,
                    "selector": selector,
                    "emitted_literal": selector,
                    "source": "selector",
                }
            )
        return f"page.locator({_py_str(selector)})"

    parsed = _parse_role_name(selector) if selector else None
    if parsed is not None:
        parsed_role, parsed_name, suffix = parsed
        # A `role=...` selector is itself an ARIA anchor (ref_to_selector form), not a native CSS path —
        # there is no underlying stable selector to prefer, so express it as get_by_role.
        if not suffix:
            return _get_by_role_expr(role or parsed_role, name or (parsed_name or ""))
        # Residual engine chain (e.g. `>> nth=`) makes the parsed form positional; anchor on role/name.
        anchor_role = role or parsed_role
        anchor_name = name or (parsed_name or "")
        if anchor_role and anchor_name:
            return _get_by_role_expr(anchor_role, anchor_name)
        return f"page.locator({_py_str(selector)})"

    if selector:
        if _is_positional_selector(selector) and role and name:
            return _get_by_role_expr(role, name)
        if _is_positional_selector(selector):
            notes.append(f"low-confidence locator: positional selector {selector!r} with no role/name to anchor on")
            return f"page.locator({_py_str(selector)})"
        if scout_ambiguous and role and name:
            return _get_by_role_expr(role, name)
        if scout_ambiguous:
            notes.append(f"disambiguated a scout-ambiguous {selector!r} selector to .first from scout document order")
            if diagnostics is not None:
                diagnostics.locator_provenance.append(
                    {
                        "trajectory_index": trajectory_index if trajectory_index is not None else -1,
                        "selector": selector,
                        "emitted_literal": selector,
                        "source": "first_fallback",
                    }
                )
            return f"page.locator({_py_str(selector)}).first"
        if _is_bare_ambiguous_selector(selector):
            if role and name:
                return _get_by_role_expr(role, name)
            notes.append(f"disambiguated a bare {selector!r} selector to .first from scout document order")
            if diagnostics is not None:
                diagnostics.locator_provenance.append(
                    {
                        "trajectory_index": trajectory_index if trajectory_index is not None else -1,
                        "selector": selector,
                        "emitted_literal": selector,
                        "source": "first_fallback",
                    }
                )
            return f"page.locator({_py_str(selector)}).first"
        return f"page.locator({_py_str(selector)})"

    if role and name:
        return _get_by_role_expr(role, name)

    notes.append("dropped an interaction with no selector and no role/name")
    return ""


def _unique_key(base: str, used: set[str]) -> str:
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _step_target(interaction: Mapping[str, Any]) -> str:
    """Plain-language anchor for a step description: accessible name, else selector, else role."""
    name = str(interaction.get("accessible_name") or "").strip()
    if name:
        return name
    selector = str(interaction.get("selector") or "").strip()
    if selector:
        return selector
    return str(interaction.get("role") or "").strip() or "the element"


def _param_key(interaction: Mapping[str, Any], used: set[str]) -> str:
    name = str(interaction.get("accessible_name") or "").strip()
    role = str(interaction.get("role") or "").strip()
    return _unique_key(_safe_param_base(name or role or "value"), used)


def _typed_value_identity(interaction: Mapping[str, Any]) -> tuple[str, str, str, str] | None:
    typed_value = str(interaction.get("typed_value") or "").strip()
    if not typed_value:
        return None
    return (
        typed_value,
        str(interaction.get("selector") or "").strip(),
        str(interaction.get("role") or "").strip(),
        str(interaction.get("accessible_name") or "").strip(),
    )


def _credential_param_key(interaction: Mapping[str, Any], used: set[str]) -> str:
    name = str(interaction.get("credential_name") or "").strip()
    return _unique_key(_safe_param_base(name or "credential"), used)


def is_durable_fallback_entry_target(interaction: Mapping[str, Any]) -> bool:
    tool_name = str(interaction.get("tool_name") or "")
    if tool_name not in _DURABLE_FALLBACK_ENTRY_TARGET_TOOLS:
        return False
    if tool_name == CREDENTIAL_FILL_TOOL_NAME:
        credential_id = str(interaction.get("credential_id") or "").strip()
        credential_field = str(interaction.get("credential_field") or "").strip()
        return bool(credential_id) and credential_field in _CREDENTIAL_FIELDS
    if tool_name == "select_option":
        return bool(str(interaction.get("value") or "").strip())
    return True


def is_generic_entry_opener_click(interaction: Mapping[str, Any]) -> bool:
    if str(interaction.get("tool_name") or "") != "click":
        return False
    if str(interaction.get("accessible_name") or "").strip():
        return False
    selector = str(interaction.get("selector") or "").strip().lower()
    role = str(interaction.get("role") or "").strip().lower()
    if role not in {"", "button"}:
        return False
    return selector == "button" or bool(re.match(r"^button(?:\\.icon|:nth-)", selector))


def _is_optional_dismissal_click(interaction: Mapping[str, Any]) -> bool:
    if str(interaction.get("tool_name") or "") != "click":
        return False
    role = str(interaction.get("role") or "").strip().lower()
    if role and role not in {"button", "link"}:
        return False
    selector = str(interaction.get("selector") or "").strip()
    name = str(interaction.get("accessible_name") or "").strip()
    if name and _OPTIONAL_DISMISSAL_NAME_PATTERN.search(name):
        return True
    return bool(selector and _OPTIONAL_DISMISSAL_SELECTOR_PATTERN.search(selector))


def _is_cookie_accept_xpath_selector(selector: str) -> bool:
    normalized_selector = selector.strip().lower()
    if normalized_selector.startswith("xpath="):
        normalized_selector = normalized_selector[len("xpath=") :].strip()
    return bool(
        _COOKIE_ACCEPT_TEXT_XPATH_PATTERN.match(normalized_selector)
        or _BODY_ROOTED_INDEXED_BUTTON_XPATH_PATTERN.match(normalized_selector)
    )


def _is_structural_dismissal_click(interaction: Mapping[str, Any]) -> bool:
    if str(interaction.get("tool_name") or "") != "click":
        return False
    role = str(interaction.get("role") or "").strip().lower()
    if role and role not in {"button", "link"}:
        return False
    if str(interaction.get("accessible_name") or "").strip():
        return False
    selector = str(interaction.get("selector") or "").strip()
    return bool(
        selector
        and (_STRUCTURAL_DISMISSAL_SELECTOR_PATTERN.search(selector) or _is_cookie_accept_xpath_selector(selector))
    )


def _has_later_durable_fallback_target(
    trajectory: Sequence[Mapping[str, Any]],
    start_index: int,
) -> bool:
    first_source_url = str(trajectory[start_index].get("source_url") or "").strip()
    for interaction in trajectory[start_index + 1 :]:
        source_url = str(interaction.get("source_url") or "").strip()
        if first_source_url and source_url and source_url != first_source_url:
            continue
        if is_durable_fallback_entry_target(interaction):
            return True
    return False


def _is_optional_or_structural_dismissal_click(interaction: Mapping[str, Any]) -> bool:
    if _is_optional_dismissal_click(interaction):
        return True
    return _is_structural_dismissal_click(interaction)


def is_optional_dismissal_only_trajectory(trajectory: Sequence[Mapping[str, Any]]) -> bool:
    return bool(trajectory) and all(
        _is_optional_or_structural_dismissal_click(interaction) for interaction in trajectory
    )


def _is_anonymous_structural_dismissal_click(interaction: Mapping[str, Any]) -> bool:
    return _is_structural_dismissal_click(interaction) and not _is_optional_dismissal_click(interaction)


def _last_action_interaction_index(trajectory: Sequence[Mapping[str, Any]]) -> int:
    last = -1
    for index, interaction in enumerate(trajectory):
        tool_name = str(interaction.get("tool_name") or "")
        if tool_name not in _ENTRY_TARGET_TOOLS:
            continue
        # An empty-key press_key is dropped as missing_key and emits nothing, so it must not claim the
        # terminal index — otherwise a trailing empty keypress steals it from a real terminal dismissal
        # click and defeats the reclassify-to-required guard.
        if tool_name == "press_key" and not str(interaction.get("key") or "").strip():
            continue
        last = index
    return last


def _optional_dismissal_locator_expr(interaction: Mapping[str, Any], fallback_locator: str) -> str:
    selector = str(interaction.get("selector") or "").strip()
    if _NOT_DECLINE_BUTTON_SELECTOR_PATTERN.match(selector) or _is_cookie_accept_xpath_selector(selector):
        # Anonymous structural/cookie-shaped selectors are brittle across reloads; name-matched dismissals
        # keep the captured locator because the accessible name is the durable anchor.
        return f"page.locator({_py_str(_COOKIE_ACCEPT_FALLBACK_LOCATOR_SELECTOR)})"
    return fallback_locator


def _should_prefer_durable_entry_target(trajectory: Sequence[Mapping[str, Any]]) -> bool:
    if not trajectory or not (
        is_generic_entry_opener_click(trajectory[0])
        or _is_optional_dismissal_click(trajectory[0])
        or _is_structural_dismissal_click(trajectory[0])
    ):
        return False
    return _has_later_durable_fallback_target(trajectory, 0)


def _code_uses_name(source: str, name: str) -> bool:
    try:
        return any(
            token.type == tokenize.NAME and token.string == name
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
        )
    except (IndentationError, tokenize.TokenError):
        return bool(re.search(rf"\b{re.escape(name)}\b", source))


def _entry_target_locator(
    trajectory: Sequence[Mapping[str, Any]], *, strict_selectors: bool, prefer_durable: bool = False
) -> tuple[str, int]:
    first_locator = ""
    first_index = -1
    for index, interaction in enumerate(trajectory):
        tool_name = str(interaction.get("tool_name") or "")
        if tool_name not in _ENTRY_TARGET_TOOLS:
            continue
        if tool_name == "press_key" and not interaction.get("selector"):
            continue
        locator = _locator_expr(interaction, [], strict_selectors=strict_selectors)
        if not locator:
            continue
        if not first_locator:
            first_locator = locator
            first_index = index
        if prefer_durable and is_durable_fallback_entry_target(interaction):
            return locator, index
        if not prefer_durable:
            return locator, index
    return first_locator, first_index


def _entry_target_locator_expr(trajectory: Sequence[Mapping[str, Any]], *, strict_selectors: bool) -> str:
    locator, _index = _entry_target_locator(trajectory, strict_selectors=strict_selectors)
    return locator


def _post_auth_resume_locator(trajectory: Sequence[Mapping[str, Any]], *, strict_selectors: bool) -> tuple[str, int]:
    last_credential_index = -1
    for index, interaction in enumerate(trajectory):
        if str(interaction.get("tool_name") or "") == CREDENTIAL_FILL_TOOL_NAME:
            last_credential_index = index
    if last_credential_index < 0:
        return "", -1

    submit_index = -1
    for index in range(last_credential_index + 1, len(trajectory)):
        if _is_submit_interaction(trajectory[index]):
            submit_index = index
            break
    if submit_index < 0:
        return "", -1

    for index in range(submit_index + 1, len(trajectory)):
        interaction = trajectory[index]
        tool_name = str(interaction.get("tool_name") or "")
        if tool_name not in _ENTRY_TARGET_TOOLS or is_generic_entry_opener_click(interaction):
            continue
        if tool_name == "press_key" and not interaction.get("selector"):
            continue
        locator = _locator_expr(interaction, [], strict_selectors=strict_selectors)
        if locator:
            return locator, index
    return "", -1


def _trajectory_prefix_at_anchor(
    trajectory: Sequence[Mapping[str, Any]], anchor: int | None
) -> tuple[Sequence[Mapping[str, Any]], int]:
    """Cut the trajectory at the position where the download affordance was observed; interactions captured
    after it navigate away, so replaying them would leave the terminal click on a page without the target."""
    if anchor is None:
        return trajectory, 0
    prefix = [
        interaction
        for interaction in trajectory
        if not isinstance(interaction.get("trajectory_index"), int) or int(interaction["trajectory_index"]) <= anchor
    ]
    if not prefix or len(prefix) == len(trajectory):
        return trajectory, 0
    return prefix, len(trajectory) - len(prefix)


def synthesize_goto_code_block(url: str) -> SynthesizedCodeBlock | None:
    """A goto-only block for a navigation with no captured interactions after it."""
    url = (url or "").strip()
    if not url:
        return None
    line = (
        f"{_INDENT}await page.goto("
        f"{_py_str(_scrub_url_for_code_literal(url))}, wait_until={_py_str(_DOMCONTENTLOADED)})"
    )
    return SynthesizedCodeBlock(
        code=line + "\n",
        steps=[{"description": f"Open {url}", "action_type": "goto_url", "line_start": 1, "line_end": 1}],
    )


def synthesize_code_block(
    trajectory: Sequence[Mapping[str, Any]],
    *,
    strict_selectors: bool = False,
    reached_download_target: ReachedDownloadTarget | None = None,
    parameter_binding_snapshot: AuthoringParameterBindingSnapshot | None = None,
) -> SynthesizedCodeBlock | None:
    """Deterministically synthesize a code block from a scout trajectory, or None if empty."""
    if not trajectory:
        return None

    lines: list[str] = []
    notes: list[str] = []
    parameters: list[dict[str, str]] = []
    diagnostics = SynthesisDiagnostics()
    steps: list[dict[str, Any]] = []
    used_param_keys: set[str] = set()
    typed_param_keys: dict[tuple[str, str, str, str], str] = {}
    credential_param_keys: dict[str, str] = {}
    used_download_vars: set[str] = set()
    grounded_binding_count = sum(1 for interaction in trajectory if "submit_rung_binding" in interaction)
    if grounded_binding_count > 1 or (grounded_binding_count and parameter_binding_snapshot is not None):
        return None
    validated_snapshot_bindings = (
        _validated_authoring_parameter_binding_snapshot(parameter_binding_snapshot, trajectory)
        if parameter_binding_snapshot is not None
        else _ValidatedSnapshotBindings({}, {})
    )
    if parameter_binding_snapshot is not None and validated_snapshot_bindings is None:
        return None
    if validated_snapshot_bindings is None:
        validated_snapshot_bindings = _ValidatedSnapshotBindings({}, {})
    snapshot_bindings_by_index = validated_snapshot_bindings.fill_by_index
    snapshot_select_option_by_index = validated_snapshot_bindings.select_option_by_index
    snapshot_recovery_bindings = (
        [binding for binding in parameter_binding_snapshot.field_bindings if binding.field_trajectory_index is None]
        if parameter_binding_snapshot is not None
        else []
    )
    compile_download_target = (
        reached_download_target is not None
        and not reached_download_target.already_registered
        and bool(reached_download_target.selector)
    )
    if compile_download_target and reached_download_target is not None:
        trajectory, dropped_trailing = _trajectory_prefix_at_anchor(
            trajectory, reached_download_target.trajectory_anchor
        )
        if dropped_trailing:
            diagnostics.download_terminal_anchor = reached_download_target.trajectory_anchor
            diagnostics.download_terminal_dropped_trailing = dropped_trailing
            LOG.info(
                "copilot_spine_download_terminal_sequenced",
                anchor=reached_download_target.trajectory_anchor,
                dropped_trailing_count=dropped_trailing,
            )
    if grounded_binding_count != sum(1 for interaction in trajectory if "submit_rung_binding" in interaction):
        return None
    diagnostics.retained_trajectory_indices = list(range(len(trajectory)))

    input_templated_keys, input_templated_needs_month = _prescan_input_templating(trajectory)
    minted_input_witness_keys: set[str] = set()
    for interaction in trajectory:
        plan = _input_templating_plan(interaction)
        if plan is None:
            continue
        for hole in plan.holes:
            key = str(hole.get("input_key") or "")
            if not key or key in minted_input_witness_keys:
                continue
            minted_input_witness_keys.add(key)
            parameters.append(
                {
                    "key": key,
                    "default_value": str(hole.get("parameter_value") or ""),
                    "source": LOCATOR_WITNESS_PARAM_SOURCE,
                }
            )
    for key in input_templated_keys:
        used_param_keys.add(key)
    if input_templated_keys:
        lines.extend(witness_prelude_lines(input_templated_keys, include_month_helper=input_templated_needs_month))
        LOG.info(
            "copilot_spine_input_templated_prelude",
            witness_keys=input_templated_keys,
            month_helper=input_templated_needs_month,
        )

    def append_step(description: str, action_type: str, line_start: int) -> None:
        steps.append(
            {
                "description": description,
                "action_type": action_type,
                "line_start": line_start,
                # last line emitted for this step; append_step always runs before the next step's lines.
                "line_end": len(lines),
            }
        )

    def record_emission(
        trajectory_index: int, tool_name: str, method: str, locator_expr: str, *, line_start: int, lane: str = ""
    ) -> None:
        record: dict[str, Any] = {
            "trajectory_index": trajectory_index,
            "tool_name": tool_name,
            "method": method,
            "selector": str(trajectory[trajectory_index].get("selector") or "").strip(),
            "locator": locator_expr,
            "call_source": textwrap.dedent("\n".join(lines[line_start - 1 :])),
        }
        if lane:
            record["lane"] = lane
        diagnostics.emitted_interactions.append(record)

    entry_url = ""
    entry_index = -1
    entry_replay_condition_active = False
    entry_replay_start_index = 0
    entry_post_auth_resume_index = 0
    login_only_presence_guard_active = False
    for index, interaction in enumerate(trajectory):
        candidate = str(interaction.get("source_url") or "").strip()
        if candidate:
            entry_url = candidate
            entry_index = index
            break
    if entry_url:
        entry_trajectory = trajectory[entry_index:]
        optional_dismissal_prefix = (
            _is_optional_or_structural_dismissal_click(entry_trajectory[0]) if entry_trajectory else False
        )
        prefer_durable_entry_target = compile_download_target or _should_prefer_durable_entry_target(entry_trajectory)
        fallback_entry_target, fallback_entry_relative_index = _entry_target_locator(
            entry_trajectory,
            strict_selectors=strict_selectors,
            prefer_durable=prefer_durable_entry_target,
        )
        if optional_dismissal_prefix and fallback_entry_relative_index == 0:
            fallback_entry_target = ""
            fallback_entry_relative_index = -1
        fallback_entry_index = (
            entry_index + fallback_entry_relative_index if fallback_entry_relative_index >= 0 else entry_index
        )
        post_auth_resume_target, post_auth_resume_relative_index = _post_auth_resume_locator(
            entry_trajectory,
            strict_selectors=strict_selectors,
        )
        entry_post_auth_resume_index = (
            entry_index + post_auth_resume_relative_index
            if post_auth_resume_relative_index > fallback_entry_relative_index
            else 0
        )
        download_entry_target = (
            f"page.locator({_py_str(reached_download_target.selector)})"
            if compile_download_target and reached_download_target is not None
            else ""
        )
        entry_target = download_entry_target if download_entry_target else fallback_entry_target
        entry_replay_condition_active = bool(download_entry_target and fallback_entry_target)
        entry_replay_start_index = (
            fallback_entry_index if fallback_entry_index > entry_index and not optional_dismissal_prefix else 0
        )
        if entry_index > 0:
            notes.append("entry URL taken from a later interaction; earlier steps had no source_url")
        if entry_replay_condition_active and fallback_entry_index > entry_index:
            notes.append("download fallback entry target taken from a later durable interaction")
        if entry_post_auth_resume_index:
            notes.append("entry fallback can resume after authentication when login controls stay hidden")
        elif fallback_entry_index > entry_index:
            notes.append("entry replay starts at a later durable interaction")
        entry_recovery_clicks: list[tuple[int, str]] = []
        if fallback_entry_index > entry_index:
            for recovery_index in range(entry_index, fallback_entry_index):
                recovery_interaction = trajectory[recovery_index]
                if not is_generic_entry_opener_click(recovery_interaction):
                    continue
                recovery_locator = _locator_expr(
                    recovery_interaction,
                    notes,
                    diagnostics=diagnostics,
                    trajectory_index=recovery_index,
                    tool_name="click",
                    strict_selectors=strict_selectors,
                )
                if recovery_locator:
                    entry_recovery_clicks.append((recovery_index, recovery_locator))
            if entry_recovery_clicks:
                notes.append("entry fallback replays a generic opener only when the durable target stays hidden")
        login_only_presence_guard_active = bool(
            entry_target
            and not entry_replay_condition_active
            and not entry_post_auth_resume_index
            and not entry_replay_start_index
            and not entry_recovery_clicks
            and any(
                str(interaction.get("tool_name") or "") == CREDENTIAL_FILL_TOOL_NAME
                and str(interaction.get("credential_field") or "").strip() in _CREDENTIAL_FIELDS
                for interaction in entry_trajectory
            )
        )
        if login_only_presence_guard_active:
            notes.append(
                "login rung fills only when the credential form is present, so an authenticated replay skips it"
            )
        line_start = len(lines) + 1
        if entry_target:
            if entry_replay_condition_active:
                lines.append(f"{_INDENT}{_ENTRY_REUSED_VAR} = False")
            if entry_post_auth_resume_index:
                lines.append(f"{_INDENT}{_ENTRY_RESUME_AFTER_AUTH_VAR} = False")
            lines.append(f"{_INDENT}{_ENTRY_TARGET_VAR} = {entry_target}")
            lines.append(f"{_INDENT}try:")
            lines.append(f'{_INDENT * 2}await {_ENTRY_TARGET_VAR}.wait_for(state="visible", timeout=1000)')
            if entry_replay_condition_active:
                lines.append(f"{_INDENT * 2}{_ENTRY_REUSED_VAR} = True")
            lines.append(f"{_INDENT}except Exception:")
            lines.append(
                f"{_INDENT * 2}await page.goto("
                f"{_py_str(_scrub_url_for_code_literal(entry_url))}, wait_until={_py_str(_DOMCONTENTLOADED)})"
            )
            post_goto_indent = 2
            if entry_replay_condition_active:
                lines.append(f"{_INDENT * 2}try:")
                lines.append(f'{_INDENT * 3}await {_ENTRY_TARGET_VAR}.wait_for(state="visible", timeout=1000)')
                lines.append(f"{_INDENT * 3}{_ENTRY_REUSED_VAR} = True")
                lines.append(f"{_INDENT * 2}except Exception:")
                post_goto_indent = 3
            if fallback_entry_target and fallback_entry_target != entry_target:
                lines.append(f"{_INDENT * post_goto_indent}{_ENTRY_TARGET_VAR} = {fallback_entry_target}")
            if entry_recovery_clicks or entry_post_auth_resume_index:
                lines.append(f"{_INDENT * post_goto_indent}try:")
                lines.append(
                    f"{_INDENT * (post_goto_indent + 1)}await {_ENTRY_TARGET_VAR}.wait_for("
                    f'state="visible", timeout=1000)'
                )
                lines.append(f"{_INDENT * post_goto_indent}except Exception:")
                if entry_post_auth_resume_index:
                    lines.append(
                        f"{_INDENT * (post_goto_indent + 1)}{_ENTRY_RESUME_TARGET_VAR} = {post_auth_resume_target}"
                    )
                    lines.append(f"{_INDENT * (post_goto_indent + 1)}try:")
                    lines.append(
                        f"{_INDENT * (post_goto_indent + 2)}await {_ENTRY_RESUME_TARGET_VAR}.wait_for("
                        f'state="visible", timeout=1000)'
                    )
                    lines.append(f"{_INDENT * (post_goto_indent + 2)}{_ENTRY_RESUME_AFTER_AUTH_VAR} = True")
                    lines.append(f"{_INDENT * (post_goto_indent + 1)}except Exception:")
                    recovery_indent = post_goto_indent + 2
                else:
                    recovery_indent = post_goto_indent + 1
                for recovery_index, recovery_locator in entry_recovery_clicks:
                    recovery_line_start = len(lines) + 1
                    lines.append(f"{_INDENT * recovery_indent}{_ENTRY_OPENER_VAR} = {recovery_locator}")
                    lines.append(f"{_INDENT * recovery_indent}if await {_ENTRY_OPENER_VAR}.count() == 1:")
                    lines.append(f"{_INDENT * (recovery_indent + 1)}await {_ENTRY_OPENER_VAR}.click()")
                    lines.append(
                        f"{_INDENT * (recovery_indent + 1)}await page.wait_for_load_state({_py_str(_DOMCONTENTLOADED)})"
                    )
                    record_emission(
                        recovery_index,
                        "click",
                        "click",
                        recovery_locator,
                        line_start=recovery_line_start,
                        lane="entry_recovery",
                    )
                lines.append(f'{_INDENT * recovery_indent}await {_ENTRY_TARGET_VAR}.wait_for(state="visible")')
            elif not login_only_presence_guard_active:
                lines.append(f'{_INDENT * post_goto_indent}await {_ENTRY_TARGET_VAR}.wait_for(state="visible")')
        else:
            lines.append(
                f"{_INDENT}await page.goto("
                f"{_py_str(_scrub_url_for_code_literal(entry_url))}, wait_until={_py_str(_DOMCONTENTLOADED)})"
            )
        if entry_replay_condition_active:
            lines.append(f"{_INDENT}if not {_ENTRY_REUSED_VAR}:")
            if entry_post_auth_resume_index:
                lines.append(f"{_INDENT * 2}if not {_ENTRY_RESUME_AFTER_AUTH_VAR}:")
                lines.append(f"{_INDENT * 3}pass")
        elif entry_post_auth_resume_index:
            lines.append(f"{_INDENT}if not {_ENTRY_RESUME_AFTER_AUTH_VAR}:")
            lines.append(f"{_INDENT * 2}pass")
        if login_only_presence_guard_active:
            lines.append(f"{_INDENT}if await {_ENTRY_TARGET_VAR}.count() == 1:")
        append_step(f"Open {entry_url}", "goto_url", line_start)

    emitted = 0
    terminal_action_index = _last_action_interaction_index(trajectory)
    deferred_readonly_assertions: list[tuple[int, str, str, str]] = []

    def action_indent_for(trajectory_index: int) -> str:
        if entry_replay_condition_active:
            if entry_post_auth_resume_index and trajectory_index < entry_post_auth_resume_index:
                return _INDENT * 3
            return _INDENT * 2
        if entry_post_auth_resume_index and trajectory_index < entry_post_auth_resume_index:
            return _INDENT * 2
        if login_only_presence_guard_active:
            return _INDENT * 2
        return _INDENT

    snapshot_recovery_emitted = False

    def emit_snapshot_recovery(trajectory_index: int, action_indent: str) -> None:
        nonlocal snapshot_recovery_emitted
        if (
            snapshot_recovery_emitted
            or parameter_binding_snapshot is None
            or _captured_trajectory_index(trajectory[trajectory_index], trajectory_index)
            != parameter_binding_snapshot.terminal.trajectory_index
        ):
            return
        for binding in snapshot_recovery_bindings:
            if binding.declared_key not in used_param_keys:
                used_param_keys.add(binding.declared_key)
                parameters.append({"key": binding.declared_key})
            lines.append(
                f"{action_indent}await page.locator({_py_str(binding.field_selector)}).fill(str({binding.declared_key}))"
            )
        diagnostics.grounded_submit_binding_fingerprints.append(parameter_binding_snapshot.fingerprint)
        snapshot_recovery_emitted = True

    for trajectory_index, interaction in enumerate(trajectory):
        if emitted >= _MAX_STEPS:
            diagnostics.truncated = True
            notes.append(f"trajectory truncated at {_MAX_STEPS} steps")
            break
        if entry_replay_start_index and trajectory_index < entry_replay_start_index:
            already_recorded = any(
                record.get("trajectory_index") == trajectory_index
                for record in (*diagnostics.emitted_interactions, *diagnostics.dropped_interactions)
            )
            if not already_recorded:
                diagnostics.forgiven_interactions.append(
                    {
                        "trajectory_index": trajectory_index,
                        "tool_name": str(interaction.get("tool_name") or ""),
                        "lane": "entry_replay_prefix",
                    }
                )
            continue
        action_indent = action_indent_for(trajectory_index)
        tool_name = str(interaction.get("tool_name") or "")

        if tool_name == "press_key":
            emit_snapshot_recovery(trajectory_index, action_indent)
            key = str(interaction.get("key") or "").strip()
            if not key:
                diagnostics.dropped_interactions.append(
                    {"trajectory_index": trajectory_index, "tool_name": tool_name, "reason_code": "missing_key"}
                )
                continue
            locator = (
                _locator_expr(
                    interaction,
                    notes,
                    diagnostics=diagnostics,
                    trajectory_index=trajectory_index,
                    tool_name=tool_name,
                    strict_selectors=strict_selectors,
                )
                if interaction.get("selector")
                else ""
            )
            line_start = len(lines) + 1
            if locator:
                lines.append(f"{action_indent}await {locator}.press({_py_str(key)})")
                record_emission(trajectory_index, tool_name, "press", locator, line_start=line_start)
            else:
                if strict_selectors:
                    diagnostics.dropped_interactions.append(
                        {
                            "trajectory_index": trajectory_index,
                            "tool_name": tool_name,
                            "reason_code": "missing_selector",
                        }
                    )
                    continue
                lines.append(f"{action_indent}await page.keyboard.press({_py_str(key)})")
                record_emission(trajectory_index, tool_name, "press", "page.keyboard", line_start=line_start)
            lines.append(f"{action_indent}await page.wait_for_load_state({_py_str(_DOMCONTENTLOADED)})")
            append_step(f"Press {key}", "keypress", line_start)
            emitted += 1
            continue

        if tool_name == "wait":
            try:
                duration_ms = int(interaction.get("duration_ms") or 0)
            except (TypeError, ValueError):
                duration_ms = 0
            if duration_ms <= 0:
                diagnostics.dropped_interactions.append(
                    {"trajectory_index": trajectory_index, "tool_name": tool_name, "reason_code": "missing_duration"}
                )
                continue
            line_start = len(lines) + 1
            lines.append(f"{action_indent}await page.wait_for_timeout({duration_ms})")
            append_step(f"Wait {max(duration_ms // 1000, 1)}s", "wait", line_start)
            emitted += 1
            continue

        locator = _locator_expr(
            interaction,
            notes,
            diagnostics=diagnostics,
            trajectory_index=trajectory_index,
            tool_name=tool_name,
            strict_selectors=strict_selectors,
        )
        if not locator:
            continue

        line_start = len(lines) + 1
        if tool_name == "click":
            emit_snapshot_recovery(trajectory_index, action_indent)
            captured_index = interaction.get("trajectory_index")
            submit_trajectory_index = (
                captured_index if isinstance(captured_index, int) and captured_index >= 0 else trajectory_index
            )
            if "submit_rung_binding" in interaction:
                grounded_binding = _validated_submit_rung_binding(interaction, submit_trajectory_index)
                if grounded_binding is None:
                    return None
                binding_fingerprint, field_bindings = grounded_binding
                for parameter_key, field_selector in field_bindings:
                    if parameter_key not in used_param_keys:
                        used_param_keys.add(parameter_key)
                        parameters.append({"key": parameter_key})
                    lines.append(
                        f"{action_indent}await page.locator({_py_str(field_selector)}).fill(str({parameter_key}))"
                    )
                diagnostics.grounded_submit_binding_fingerprints.append(binding_fingerprint)
            reclassify_terminal_required = (
                trajectory_index == terminal_action_index
                and _is_anonymous_structural_dismissal_click(interaction)
                and any(not str(record.get("lane") or "") for record in diagnostics.emitted_interactions)
            )
            if _is_optional_or_structural_dismissal_click(interaction) and not reclassify_terminal_required:
                optional_locator = _optional_dismissal_locator_expr(interaction, locator)
                lines.append(f"{action_indent}{_OPTIONAL_DISMISSAL_VAR} = {optional_locator}")
                lines.append(f"{action_indent}if await {_OPTIONAL_DISMISSAL_VAR}.count() > 0:")
                lines.append(f"{action_indent}{_INDENT}try:")
                lines.append(f"{action_indent}{_INDENT * 2}await {_OPTIONAL_DISMISSAL_VAR}.first.click(timeout=1000)")
                lines.append(
                    f"{action_indent}{_INDENT * 2}await page.wait_for_load_state({_py_str(_DOMCONTENTLOADED)})"
                )
                lines.append(f"{action_indent}{_INDENT}except Exception:")
                lines.append(f"{action_indent}{_INDENT * 2}pass")
                record_emission(
                    trajectory_index,
                    tool_name,
                    "click",
                    optional_locator,
                    line_start=line_start,
                    lane="optional_dismissal",
                )
            else:
                lines.append(f"{action_indent}await {locator}.click()")
                lines.append(f"{action_indent}await page.wait_for_load_state({_py_str(_DOMCONTENTLOADED)})")
                record_emission(trajectory_index, tool_name, "click", locator, line_start=line_start)
            append_step(f"Click {_step_target(interaction)}", "click", line_start)
        elif tool_name == "type_text":
            snapshot_binding = snapshot_bindings_by_index.get(trajectory_index)
            typed_identity = _typed_value_identity(interaction)
            param_key = snapshot_binding[0] if snapshot_binding is not None else None
            if param_key is None:
                param_key = typed_param_keys.get(typed_identity) if typed_identity is not None else None
            if param_key is None or param_key not in used_param_keys:
                if param_key is None:
                    param_key = _param_key(interaction, used_param_keys)
                else:
                    used_param_keys.add(param_key)
                parameter = {"key": param_key}
                typed_value = str(interaction.get("typed_value") or "").strip()
                if typed_value and snapshot_binding is None:
                    parameter["default_value"] = typed_value
                typed_length = interaction.get("typed_length")
                if strict_selectors and typed_length is not None and snapshot_binding is None:
                    try:
                        typed_length_int = int(typed_length)
                    except (TypeError, ValueError):
                        typed_length_int = 0
                    if typed_length_int > 0:
                        parameter["typed_length"] = str(typed_length_int)
                parameters.append(parameter)
                if typed_identity is not None:
                    typed_param_keys[typed_identity] = param_key
            diagnostics.typed_param_bindings.append((trajectory_index, param_key))
            readonly_or_disabled = bool(interaction.get("control_readonly")) or bool(
                interaction.get("control_disabled")
            )
            if readonly_or_disabled and bool(interaction.get("control_value_satisfied")):
                verify_target = _step_target(interaction)
                lines.append(f"{action_indent}try:")
                lines.append(f"{action_indent}{_INDENT}{_READONLY_DEFERRED_VAR} = await {locator}.input_value()")
                lines.append(f"{action_indent}except Exception:")
                lines.append(f"{action_indent}{_INDENT}{_READONLY_DEFERRED_VAR} = None")
                lines.append(
                    f"{action_indent}if {_READONLY_DEFERRED_VAR} is not None "
                    f"and {_READONLY_DEFERRED_VAR} != str({param_key}):"
                )
                lines.append(
                    f"{action_indent}{_INDENT}print("
                    f"{_py_str(f'{verify_target}: read-only value ')} + repr({_READONLY_DEFERRED_VAR})"
                    f" + {_py_str(' does not match expected ')} + repr(str({param_key})))"
                )
                record_emission(
                    trajectory_index, tool_name, "input_value", locator, line_start=line_start, lane="readonly_skip"
                )
                append_step(f"Verify {verify_target}", "input_text", line_start)
            elif readonly_or_disabled:
                deferred_readonly_assertions.append((trajectory_index, locator, param_key, _step_target(interaction)))
            else:
                lines.append(f"{action_indent}await {locator}.fill(str({param_key}))")
                record_emission(trajectory_index, tool_name, "fill", locator, line_start=line_start)
                append_step(f"Type into {_step_target(interaction)}", "input_text", line_start)
        elif tool_name == CREDENTIAL_FILL_TOOL_NAME:
            credential_id = str(interaction.get("credential_id") or "").strip()
            credential_field = str(interaction.get("credential_field") or "").strip()
            if not credential_id or credential_field not in _CREDENTIAL_FIELDS:
                notes.append("dropped a credential fill with no usable credential reference")
                diagnostics.dropped_interactions.append(
                    {
                        "trajectory_index": trajectory_index,
                        "tool_name": tool_name,
                        "reason_code": "missing_credential_reference",
                    }
                )
                continue
            credential_param_key = credential_param_keys.get(credential_id)
            if credential_param_key is None:
                credential_param_key = _credential_param_key(interaction, used_param_keys)
                credential_param_keys[credential_id] = credential_param_key
                parameters.append({"key": credential_param_key, "credential_id": credential_id})
            if credential_field == "totp":
                lines.append(f"{action_indent}await {locator}.fill(await {credential_param_key}.otp())")
            else:
                lines.append(f"{action_indent}await {locator}.fill({credential_param_key}.{credential_field})")
            record_emission(trajectory_index, tool_name, "fill", locator, line_start=line_start)
        elif tool_name == "select_option":
            emit_snapshot_recovery(trajectory_index, action_indent)
            value = str(interaction.get("value") or "").strip()
            if not value:
                notes.append("dropped a select_option interaction with no recorded value")
                diagnostics.dropped_interactions.append(
                    {"trajectory_index": trajectory_index, "tool_name": tool_name, "reason_code": "missing_value"}
                )
                continue
            bound_key = snapshot_select_option_by_index.get(trajectory_index)
            if bound_key is not None:
                if bound_key not in used_param_keys:
                    used_param_keys.add(bound_key)
                    parameters.append({"key": bound_key})
                lines.append(f"{action_indent}await {locator}.select_option(str({bound_key}))")
            else:
                lines.append(f"{action_indent}await {locator}.select_option({_py_str(value)})")
            lines.append(f"{action_indent}await page.wait_for_load_state({_py_str(_DOMCONTENTLOADED)})")
            record_emission(trajectory_index, tool_name, "select_option", locator, line_start=line_start)
            append_step(f"Select {value} in {_step_target(interaction)}", "select_option", line_start)
        elif tool_name == "hover" and not strict_selectors:
            # Non-strict only: recording trajectories carry deliberate hovers; the
            # strict-imposition envelope keeps treating hover as unsupported.
            lines.append(f"{action_indent}await {locator}.hover()")
            append_step(f"Hover over {_step_target(interaction)}", "hover", line_start)
        else:
            notes.append(f"skipped unsupported interaction tool_name={tool_name!r}")
            diagnostics.dropped_interactions.append(
                {"trajectory_index": trajectory_index, "tool_name": tool_name, "reason_code": "unsupported_tool"}
            )
            continue
        emitted += 1

    if (
        entry_replay_condition_active
        and (emitted - len(deferred_readonly_assertions)) == 0
        and (not entry_post_auth_resume_index)
    ):
        lines.append(f"{_INDENT * 2}pass")

    if login_only_presence_guard_active and (emitted - len(deferred_readonly_assertions)) == 0:
        lines.append(f"{_INDENT * 2}pass")

    if deferred_readonly_assertions:
        deferred_base = _INDENT
        if entry_replay_condition_active:
            lines.append(f"{_INDENT}if not {_ENTRY_REUSED_VAR}:")
            deferred_base = _INDENT * 2

        def emit_deferred_readonly_assertion(indent: str, locator_expr: str, param_ref: str, target: str) -> None:
            line_start = len(lines) + 1
            lines.append(f"{indent}try:")
            lines.append(f"{indent}{_INDENT}{_READONLY_DEFERRED_VAR} = await {locator_expr}.input_value()")
            lines.append(f"{indent}except Exception:")
            lines.append(f"{indent}{_INDENT}{_READONLY_DEFERRED_VAR} = None")
            lines.append(f"{indent}if {_READONLY_DEFERRED_VAR} == {_py_str('')}:")
            lines.append(
                f"{indent}{_INDENT}raise AssertionError("
                f"{_py_str(f'{target} was not set to the required value by an earlier step')})"
            )
            lines.append(
                f"{indent}elif {_READONLY_DEFERRED_VAR} is not None and {_READONLY_DEFERRED_VAR} != str({param_ref}):"
            )
            lines.append(
                f"{indent}{_INDENT}print("
                f"{_py_str(f'{target}: read-only value ')} + repr({_READONLY_DEFERRED_VAR})"
                f" + {_py_str(' does not match expected ')} + repr(str({param_ref})))"
            )
            append_step(f"Verify {target}", "input_text", line_start)

        for deferred_index, deferred_locator, deferred_param_key, deferred_target in deferred_readonly_assertions:
            if entry_post_auth_resume_index and deferred_index < entry_post_auth_resume_index:
                continue
            deferred_line_start = len(lines) + 1
            emit_deferred_readonly_assertion(deferred_base, deferred_locator, deferred_param_key, deferred_target)
            record_emission(
                deferred_index,
                "type_text",
                "input_value",
                deferred_locator,
                line_start=deferred_line_start,
                lane="readonly_skip",
            )

        pre_resume_deferred = [
            entry
            for entry in deferred_readonly_assertions
            if entry_post_auth_resume_index and entry[0] < entry_post_auth_resume_index
        ]
        if pre_resume_deferred:
            lines.append(f"{deferred_base}if not {_ENTRY_RESUME_AFTER_AUTH_VAR}:")
            for deferred_index, deferred_locator, deferred_param_key, deferred_target in pre_resume_deferred:
                deferred_line_start = len(lines) + 1
                emit_deferred_readonly_assertion(
                    deferred_base + _INDENT, deferred_locator, deferred_param_key, deferred_target
                )
                record_emission(
                    deferred_index,
                    "type_text",
                    "input_value",
                    deferred_locator,
                    line_start=deferred_line_start,
                    lane="readonly_skip",
                )

    if compile_download_target and reached_download_target is not None:
        # The download affordance is observed in nav_targets, not necessarily a trajectory click, so the
        # download is an appended terminal step compiled from the typed target — never an in-place click upgrade.
        # Awaiting the download value lands the file in the run-scoped downloads dir; the execution-layer
        # dir-diff registers the single file, so the synthesizer never save_as (which would double-register).
        # Return a JSON-safe filename summary too; artifact IDs/URLs are injected by the execution layer.
        download_var = _unique_key(_DOWNLOAD_VAR_BASE, used_download_vars)
        download_obj = _unique_key(f"{download_var}_file", used_download_vars)
        download_filename = _unique_key(_DOWNLOAD_FILENAME_VAR_BASE, used_download_vars)
        lines.append(f"{_INDENT}async with page.expect_download() as {download_var}:")
        lines.append(f"{_INDENT * 2}await page.locator({_py_str(reached_download_target.selector)}).click()")
        lines.append(f"{_INDENT}{download_obj} = await {download_var}.value")
        lines.append(f"{_INDENT}{download_filename} = {download_obj}.suggested_filename")
        lines.append(f"{_INDENT}await {download_obj}.path()")
        lines.append(f"{_INDENT}return {{")
        lines.append(f'{_INDENT * 2}"downloaded_file_name": {download_filename},')
        lines.append(f"{_INDENT}}}")

    if not lines:
        return None
    expected_binding_fingerprint_count = grounded_binding_count + (1 if parameter_binding_snapshot is not None else 0)
    if len(diagnostics.grounded_submit_binding_fingerprints) != expected_binding_fingerprint_count:
        return None
    emitted_code = "\n".join(lines)
    for scout_var in _INTERNAL_SCOUT_VARS:
        if not _code_uses_name(emitted_code, scout_var):
            continue
        # Code-block safe vars expose `Exception`, not `NameError`; this cleanup only
        # swallows missing generated scout locals after guarded branches.
        lines.append(f"{_INDENT}try:")
        lines.append(f"{_INDENT * 2}del {scout_var}")
        lines.append(f"{_INDENT}except Exception:")
        lines.append(f"{_INDENT * 2}pass")
    if steps:
        steps[-1]["line_end"] = len(lines)

    diagnostics.emitted_interaction_count = emitted
    code = "\n".join(lines) + "\n"
    return SynthesizedCodeBlock(code=code, parameters=parameters, notes=notes, diagnostics=diagnostics, steps=steps)


SCOUTED_SPINE_UNDER_BUILD_REASON_CODE = "scouted_spine_under_build"
SCOUTED_SPINE_DROPPED_UNFORGIVEN_REASON_CODE = "scouted_spine_dropped_unforgiven"
SCOUTED_SPINE_UNRECORDED_INDEX_REASON_CODE = "scouted_spine_unrecorded_index"
SCOUTED_SPINE_TRUNCATED_REASON_CODE = "scouted_spine_truncated"


_LEADING_TAG_ID_SELECTOR_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*#")


def normalized_scout_selector(selector: str) -> str:
    # Capture and persist-seam comparison share one normal form: a leading `tag#id` qualifier reduces
    # to `#id` (ids are document-unique), so both sides name the same control.
    return _LEADING_TAG_ID_SELECTOR_RE.sub("#", selector)


def normalized_locator_expr(text: str) -> str:
    try:
        return ast.unparse(ast.parse(text, mode="eval"))
    except SyntaxError:
        return text


def locator_selector_literals(locator: str) -> set[str]:
    try:
        tree = ast.parse(locator, mode="eval")
    except SyntaxError:
        return set()
    return {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}


def _bare_locator_call_selector(receiver: str) -> str | None:
    try:
        node = ast.parse(receiver, mode="eval").body
    except SyntaxError:
        return None
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "locator"
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.args[0].value
    return None


def emitted_record_covered_by_call(record: Mapping[str, Any], method: str, receiver: str) -> bool:
    if method != str(record.get("method") or ""):
        return False
    locator = str(record.get("locator") or "")
    if locator and normalized_locator_expr(receiver) == normalized_locator_expr(locator):
        return True
    # Literal-membership matching would falsely cover a receiver that merely quotes the captured
    # selector (a shared name= across different elements), so only a bare .locator(<selector>) counts.
    selector = str(record.get("selector") or "")
    return bool(selector) and selector == _bare_locator_call_selector(receiver)


def uncovered_required_emitted_interactions(
    emitted_interactions: Sequence[Mapping[str, Any]],
    draft_calls: Sequence[tuple[str, str]],
) -> list[Mapping[str, Any]]:
    """Required (non-lane) emitted records the draft's ordered (method, receiver) calls do not
    cover as an ordered subsequence, matched at method + selector/locator level."""
    required = [record for record in emitted_interactions if not str(record.get("lane") or "")]
    if not required:
        return []
    # Greedy ordered-subsequence scan: a miss consumes the remaining calls, so later rungs over-report (safe superset).
    uncovered: list[Mapping[str, Any]] = []
    next_call_index = 0
    for record in required:
        match_index = None
        for call_index in range(next_call_index, len(draft_calls)):
            method, receiver = draft_calls[call_index]
            if emitted_record_covered_by_call(record, method, receiver):
                match_index = call_index
                break
        if match_index is None:
            uncovered.append(record)
            next_call_index = len(draft_calls)
        else:
            next_call_index = match_index + 1
    return uncovered


_IDENTITY_QUALIFIER_BOUNDARY = ("[", "#", ".")
_FILTERING_PSEUDO_CLASSES = (
    ":visible",
    ":enabled",
    ":disabled",
    ":checked",
    ":not(",
    ":has(",
    ":has-text(",
    ":text(",
    ":is(",
)
_EXACT_TEXT_XPATH_TAG_RE = re.compile(
    r"""^(?:xpath=)?//(?P<tag>[a-zA-Z][a-zA-Z0-9-]*)\s*\[\s*normalize-space\(\s*(?:\.|text\(\))?\s*\)\s*=\s*(?P<quote>['"])[^'"]+(?P=quote)\s*\]\s*$"""
)


def _qualifier_narrows_to_identity(qualifier: str) -> bool:
    if not qualifier or qualifier[0] not in _IDENTITY_QUALIFIER_BOUNDARY:
        return False
    if any(pseudo in qualifier for pseudo in _FILTERING_PSEUDO_CLASSES):
        return False
    bracket_depth = 0
    quote: str | None = None
    for char in qualifier:
        if quote is not None:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == "[":
            bracket_depth += 1
        elif char == "]":
            bracket_depth = max(0, bracket_depth - 1)
        elif bracket_depth == 0 and (char.isspace() or char in ">+~"):
            return False
    return True


def _selector_refines(bare: str, candidate: str) -> bool:
    bare = bare.strip()
    candidate = candidate.strip()
    if not bare or not candidate or bare == candidate:
        return False

    bare_role = _parse_role_name(bare)
    candidate_role = _parse_role_name(candidate)
    if bare_role is not None or candidate_role is not None:
        if bare_role is None or candidate_role is None:
            return False
        bare_role_name, bare_name, bare_suffix = bare_role
        candidate_role_name, candidate_name, candidate_suffix = candidate_role
        return (
            bare_role_name == candidate_role_name
            and not bare_name
            and not bare_suffix
            and bool(candidate_name)
            and not candidate_suffix
        )
    if not _BARE_TAG_RE.match(bare):
        return False
    if not candidate.startswith(bare) or _is_positional_selector(candidate):
        return False
    return _qualifier_narrows_to_identity(candidate[len(bare) :])


def _stable_same_kind_bare_click_refiner(bare: str, candidate: str) -> bool:
    bare = bare.strip()
    candidate = candidate.strip()
    if not bare or not candidate or bare == candidate or _is_positional_selector(candidate):
        return False
    if _selector_refines(bare, candidate):
        return True
    if bare != "button":
        return False

    candidate_role = _parse_role_name(candidate)
    if candidate_role is not None:
        role_name, accessible_name, suffix = candidate_role
        return role_name == "button" and bool(accessible_name) and not suffix

    xpath_match = _EXACT_TEXT_XPATH_TAG_RE.match(candidate)
    return xpath_match is not None and xpath_match.group("tag").casefold() == "button"


def _is_ignorable_entry_opener_drop(dropped: Mapping[str, Any], diagnostics: SynthesisDiagnostics) -> bool:
    return (
        dropped.get("reason_code") == "ambiguous_bare_selector"
        and dropped.get("tool_name") == "click"
        and dropped.get("trajectory_index") == 0
        and str(dropped.get("selector") or "").strip() in {"button", "role=button"}
        and bool(diagnostics.locator_provenance)
    )


def _bare_drop_superseded_on_screen(
    dropped: Mapping[str, Any],
    scout_trajectory: Sequence[Mapping[str, Any]],
    *,
    claimed_refiner_indices: set[int],
) -> tuple[bool, dict[str, Any] | None]:
    if dropped.get("reason_code") != "ambiguous_bare_selector" or dropped.get("tool_name") != "click":
        return False, None
    dropped_selector = str(dropped.get("selector") or "").strip()
    if not dropped_selector:
        return False, None

    dropped_index = dropped.get("trajectory_index")
    if not isinstance(dropped_index, int) or dropped_index < 0 or dropped_index >= len(scout_trajectory):
        return False, None
    source_url = str(scout_trajectory[dropped_index].get("source_url") or "").strip()
    if not source_url:
        return False, None

    for refiner_index in range(dropped_index + 1, len(scout_trajectory)):
        if refiner_index in claimed_refiner_indices:
            continue
        later = scout_trajectory[refiner_index]
        if later.get("tool_name") != "click":
            continue
        if str(later.get("source_url") or "").strip() != source_url:
            continue
        later_selector = str(later.get("selector") or "").strip()
        if not _stable_same_kind_bare_click_refiner(dropped_selector, later_selector):
            continue
        claimed_refiner_indices.add(refiner_index)
        return True, {
            "dropped_index": dropped_index,
            "dropped_selector": dropped_selector,
            "refiner_index": refiner_index,
            "refiner_selector": later_selector,
            "source_url": source_url,
        }
    return False, None


UNCOVERED_RUNG_FINDING = "uncovered_rung"
UNFORGIVEN_DROP_FINDING = "unforgiven_drop"
UNRECORDED_INDEX_FINDING = "unrecorded_index"
TRUNCATED_FINDING = "truncated"


@dataclass(frozen=True, slots=True)
class ObligationFinding:
    kind: str
    record: Mapping[str, Any] | None = None
    trajectory_index: int | None = None


def forgiven_dropped_indices(
    diagnostics: SynthesisDiagnostics, scout_trajectory: Sequence[Mapping[str, Any]]
) -> set[int]:
    """Trajectory indices whose drop the closed forgiveness allowlist absolves, re-derived from the
    synthesized diagnostics and trajectory so no forgiveness record needs to be transported."""
    forgiven: set[int] = set()
    claimed_refiner_indices: set[int] = set()
    for dropped in diagnostics.dropped_interactions:
        index = dropped.get("trajectory_index")
        if _is_ignorable_entry_opener_drop(dropped, diagnostics):
            if isinstance(index, int):
                forgiven.add(index)
            continue
        superseded, _ = _bare_drop_superseded_on_screen(
            dropped, scout_trajectory, claimed_refiner_indices=claimed_refiner_indices
        )
        if superseded and isinstance(index, int):
            forgiven.add(index)
    return forgiven


def _recorded_partition_indices(diagnostics: SynthesisDiagnostics) -> set[int]:
    recorded: set[int] = set()
    for group in (
        diagnostics.emitted_interactions,
        diagnostics.dropped_interactions,
        diagnostics.forgiven_interactions,
    ):
        for record in group:
            index = record.get("trajectory_index")
            if isinstance(index, int):
                recorded.add(index)
    return recorded


def spine_partition_findings(
    diagnostics: SynthesisDiagnostics,
    draft_calls: Sequence[tuple[str, str]],
    scout_trajectory: Sequence[Mapping[str, Any]],
) -> list[ObligationFinding]:
    """Partition-exhaustiveness obligation over the full retained-index manifest: an uncovered required
    rung, a dropped interaction the allowlist does not forgive, a retained index in no record lane, or a
    truncation are each a typed under-build finding. Forgiveness names the reason; it never absolves."""
    findings: list[ObligationFinding] = []
    for record in uncovered_required_emitted_interactions(diagnostics.emitted_interactions, draft_calls):
        index = record.get("trajectory_index")
        findings.append(
            ObligationFinding(
                kind=UNCOVERED_RUNG_FINDING,
                record=record,
                trajectory_index=index if isinstance(index, int) else None,
            )
        )
    forgiven = forgiven_dropped_indices(diagnostics, scout_trajectory)
    for dropped in diagnostics.dropped_interactions:
        index = dropped.get("trajectory_index")
        if isinstance(index, int) and index in forgiven:
            continue
        findings.append(
            ObligationFinding(
                kind=UNFORGIVEN_DROP_FINDING,
                record=dropped,
                trajectory_index=index if isinstance(index, int) else None,
            )
        )
    recorded = _recorded_partition_indices(diagnostics)
    for index in diagnostics.retained_trajectory_indices:
        if index not in recorded:
            findings.append(ObligationFinding(kind=UNRECORDED_INDEX_FINDING, trajectory_index=index))
    if diagnostics.truncated:
        findings.append(ObligationFinding(kind=TRUNCATED_FINDING))
    return findings


def uncovered_rung_records(findings: Sequence[ObligationFinding]) -> list[Mapping[str, Any]]:
    return [finding.record for finding in findings if finding.kind == UNCOVERED_RUNG_FINDING and finding.record]


def obligation_finding_reason_code(finding: ObligationFinding) -> str:
    if finding.kind == UNCOVERED_RUNG_FINDING:
        return SCOUTED_SPINE_UNDER_BUILD_REASON_CODE
    if finding.kind == UNFORGIVEN_DROP_FINDING:
        return SCOUTED_SPINE_DROPPED_UNFORGIVEN_REASON_CODE
    if finding.kind == UNRECORDED_INDEX_FINDING:
        return SCOUTED_SPINE_UNRECORDED_INDEX_REASON_CODE
    return SCOUTED_SPINE_TRUNCATED_REASON_CODE


def obligation_finding_selector(finding: ObligationFinding) -> str | None:
    if finding.record is None:
        return None
    return str(finding.record.get("selector") or "") or None


def obligation_finding_text(finding: ObligationFinding) -> str:
    if finding.kind == UNCOVERED_RUNG_FINDING:
        return missing_rung_text([finding.record]) if finding.record else "an uncovered scouted rung"
    if finding.kind == UNFORGIVEN_DROP_FINDING:
        record = finding.record or {}
        tool_name = str(record.get("tool_name") or "unknown")
        reason = str(record.get("reason_code") or "unknown")
        index = record.get("trajectory_index", "?")
        return f"dropped scout interaction {index} from `{tool_name}` ({reason})"
    if finding.kind == UNRECORDED_INDEX_FINDING:
        return f"scout interaction {finding.trajectory_index} was retained but landed in no persisted or forgiven lane"
    return "the scout trajectory was truncated before every captured interaction was compiled"


def render_obligation_findings(findings: Sequence[ObligationFinding]) -> str:
    return "; ".join(obligation_finding_text(finding) for finding in findings)


def missing_rung_text(uncovered: Sequence[Mapping[str, Any]]) -> str:
    return ", ".join(
        f"`{str(record.get('method') or '')}` on {str(record.get('selector') or record.get('locator') or '')!r}"
        for record in uncovered
    )


def render_missing_rung_call_sources(uncovered: Sequence[Mapping[str, Any]]) -> str:
    sources = [source for record in uncovered if (source := str(record.get("call_source") or "").strip())]
    if not sources:
        return ""
    return "Missing rung source to reuse verbatim:\n```python\n" + "\n".join(sources) + "\n```"


def _return_node_expression(node: _ExtractionReturnNode) -> str:
    if node.value_expression:
        return node.value_expression
    items = ", ".join(
        f"{json.dumps(key)}: {_return_node_expression(child)}" for key, child in sorted(node.children.items())
    )
    return "{" + items + "}"


def _set_return_expression(root: _ExtractionReturnNode, segments: Sequence[tuple[str, bool]], expression: str) -> None:
    current = root
    for name, _is_array in segments:
        current = current.children.setdefault(name, _ExtractionReturnNode())
    current.value_expression = expression


def _array_prefix_of_segments(segments: Sequence[tuple[str, bool]]) -> tuple[tuple[str, bool], ...]:
    for index, (_name, is_array) in enumerate(segments):
        if is_array:
            return tuple(segments[: index + 1])
    return ()


def _array_prefix(binding: LiveReadBinding) -> tuple[tuple[str, bool], ...]:
    return _array_prefix_of_segments(output_path_segments(binding.output_path))


def _key_value_scalar_read_statements(binding: LiveReadBinding, variable: str, *, guard_empty: bool) -> list[str]:
    container = f"page.locator({json.dumps(binding.selector)})"
    target = f"{container}.nth({binding.selector_index})"
    children = f'{target}.locator(":scope > *")'
    statements = [
        f"if await {container}.count() != {binding.selector_count}:",
        f'{_INDENT}raise ValueError("Observed scalar selector cardinality changed")',
        f"if not await {target}.is_visible():",
        f'{_INDENT}raise ValueError("Observed scalar relation is no longer visible")',
        f"if await {children}.count() != {binding.child_count}:",
        f'{_INDENT}raise ValueError("Observed scalar direct-child shape changed")',
        f"if not await {children}.nth(0).is_visible():",
        f'{_INDENT}raise ValueError("Observed scalar label is no longer visible")',
        f"if (await {children}.nth(0).inner_text()).strip() != {json.dumps(binding.relation_label)}:",
        f'{_INDENT}raise ValueError("Observed scalar label changed")',
        f"if not await {children}.nth({binding.child_index}).is_visible():",
        f'{_INDENT}raise ValueError("Observed scalar value is no longer visible")',
        f"{variable} = (await {children}.nth({binding.child_index}).inner_text()).strip()",
    ]
    if guard_empty:
        statements.extend(
            [
                f"if not {variable}:",
                f'{_INDENT}raise ValueError("Observed scalar value is empty")',
            ]
        )
    return statements


def _table_group_read_lines(
    bindings: list[LiveReadBinding],
    *,
    row_selector: str,
    row_count: int,
    prefix: tuple[tuple[str, bool], ...],
    group_index: int,
    records_variable: str,
    cell_variable_base: str,
    assemble_as_literal: bool,
    guard_empty: bool = False,
    none_leaf_segments: Sequence[tuple[tuple[str, bool], ...]] = (),
) -> list[str]:
    lines: list[str] = []
    record_root = _ExtractionReturnNode()
    for none_segments in none_leaf_segments:
        _set_return_expression(record_root, none_segments, "None")
    exemplar = bindings[0]
    table = f"page.locator({json.dumps(exemplar.selector)})"
    selected_table = f"{table}.nth({exemplar.selector_index})"
    rows = f"page.locator({json.dumps(row_selector)})"
    headers = f'{table}.nth({exemplar.selector_index}).locator(":scope > thead > tr > th")'
    lines.append(f"if await {table}.count() != {exemplar.selector_count}:")
    lines.append(f'{_INDENT}raise ValueError("Observed table identity changed")')
    lines.append(f"if not await {table}.nth({exemplar.selector_index}).is_visible():")
    lines.append(f'{_INDENT}raise ValueError("Observed table is no longer visible")')
    lines.append(f'if await {selected_table}.locator(":scope table").count() != 0:')
    lines.append(f'{_INDENT}raise ValueError("Observed table gained a nested table")')
    lines.append(f'if await {table}.nth({exemplar.selector_index}).locator("[colspan], [rowspan]").count() != 0:')
    lines.append(f'{_INDENT}raise ValueError("Observed table gained spanning cells")')
    lines.append(f"if await {headers}.count() != {len(exemplar.headers)}:")
    lines.append(f'{_INDENT}raise ValueError("Observed table header cardinality changed")')
    for header_index, header_text in enumerate(exemplar.headers):
        lines.append(f"if (await {headers}.nth({header_index}).inner_text()).strip() != {json.dumps(header_text)}:")
        lines.append(f'{_INDENT}raise ValueError("Observed table header identity changed")')
    lines.append(f"if await {rows}.count() != {row_count}:")
    lines.append(f'{_INDENT}raise ValueError("Observed table row count changed")')
    row_expressions: list[str] = []
    if not assemble_as_literal:
        lines.append(f"{records_variable} = []")
    for row_index in range(row_count):
        row = f"{rows}.nth({row_index})"
        cells = f'{row}.locator(":scope > th, :scope > td")'
        lines.append(f"if not await {row}.is_visible():")
        lines.append(f'{_INDENT}raise ValueError("Observed table row is no longer visible")')
        lines.append(f"if await {cells}.count() != {exemplar.row_cell_counts[row_index]}:")
        lines.append(f'{_INDENT}raise ValueError("Observed table direct-cell cardinality changed")')
        lines.append(f'if await {row}.locator(":scope > th").count() != 0:')
        lines.append(f'{_INDENT}raise ValueError("Observed table row gained a row header")')
        lines.append(
            f'if " ".join((await {row}.inner_text()).split()) != {json.dumps(exemplar.row_identities[row_index])}:'
        )
        lines.append(f'{_INDENT}raise ValueError("Observed table row identity changed")')
        for binding_index, binding in enumerate(sorted(bindings, key=lambda item: item.output_path)):
            value_variable = f"{cell_variable_base}_{group_index}_{row_index}_{binding_index}"
            lines.append(f"if not await {cells}.nth({binding.column_index}).is_visible():")
            lines.append(f'{_INDENT}raise ValueError("Observed table cell is no longer visible")')
            lines.append(f"{value_variable} = (await {cells}.nth({binding.column_index}).inner_text()).strip()")
            if guard_empty:
                lines.append(f"if not {value_variable}:")
                lines.append(f'{_INDENT}raise ValueError("Observed table cell value is empty")')
            _set_return_expression(
                record_root, output_path_segments(binding.output_path)[len(prefix) :], value_variable
            )
        row_expression = _return_node_expression(record_root)
        if assemble_as_literal:
            row_expressions.append(row_expression)
        else:
            lines.append(f"{records_variable}.append({row_expression})")
    if assemble_as_literal:
        lines.append(f"{records_variable} = [{', '.join(row_expressions)}]")
    return lines


def synthesize_extraction_suffix(plan: RequestedOutputExtractionPlan) -> SynthesizedExtractionSuffix | None:
    if not plan.live_reads:
        return None
    lines: list[str] = []
    return_root = _ExtractionReturnNode()
    scalar_bindings = [binding for binding in plan.live_reads if binding.kind == LiveReadKind.KEY_VALUE]
    for index, binding in enumerate(scalar_bindings):
        variable = f"_extraction_value_{index}"
        lines.extend(_key_value_scalar_read_statements(binding, variable, guard_empty=False))
        _set_return_expression(return_root, output_path_segments(binding.output_path), variable)

    table_groups: dict[tuple[str, int, tuple[tuple[str, bool], ...]], list[LiveReadBinding]] = {}
    for binding in plan.live_reads:
        if binding.kind != LiveReadKind.TABLE_COLUMN:
            continue
        prefix = _array_prefix(binding)
        if not prefix or not binding.row_selector or binding.row_count <= 0:
            return None
        table_groups.setdefault((binding.row_selector, binding.row_count, prefix), []).append(binding)
    for group_index, ((row_selector, row_count, prefix), bindings) in enumerate(sorted(table_groups.items())):
        records_variable = f"_extraction_records_{group_index}"
        lines.extend(
            _table_group_read_lines(
                bindings,
                row_selector=row_selector,
                row_count=row_count,
                prefix=prefix,
                group_index=group_index,
                records_variable=records_variable,
                cell_variable_base="_extraction_cell",
                assemble_as_literal=False,
            )
        )
        _set_return_expression(return_root, prefix, records_variable)

    lines.append(f"return {_return_node_expression(return_root)}")
    code = "\n".join(lines) + "\n"
    fingerprint_material = repr((plan.identity, plan.observation_identity, plan.reveal, code))
    return SynthesizedExtractionSuffix(code=code, fingerprint=hashlib.sha256(fingerprint_material.encode()).hexdigest())


_ENVELOPE_SCALAR_VAR_BASE = "_envelope_value"
_ENVELOPE_CELL_VAR_BASE = "_envelope_cell"
_ENVELOPE_RECORDS_VAR_BASE = "_envelope_records"


@dataclass(frozen=True, slots=True)
class ProducedStaticReturnEnvelope:
    code: str
    keyed_paths: tuple[str, ...]


def _snippet_scope_returns(statements: Sequence[ast.stmt]) -> list[ast.Return]:
    found: list[ast.Return] = []
    for statement in statements:
        if isinstance(statement, ast.Return):
            found.append(statement)
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for child in ast.iter_child_nodes(statement):
            if isinstance(child, ast.stmt):
                found.extend(_snippet_scope_returns([child]))
            elif isinstance(child, (ast.ExceptHandler, ast.match_case)):
                found.extend(_snippet_scope_returns(child.body))
    return found


def _bound_or_referenced_identifiers(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                names.add(node.name)
        elif isinstance(node, ast.alias):
            if node.asname:
                names.add(node.asname)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
        elif isinstance(node, ast.MatchAs):
            if node.name:
                names.add(node.name)
    return names


def produce_covered_static_return_envelope(
    code: str,
    *,
    plan: RequestedOutputExtractionPlan | None,
    scalar_required_paths: set[str],
    declaration_paths: set[str],
    download_required_paths: set[str],
    expects_download: bool,
) -> ProducedStaticReturnEnvelope | None:
    """Author the unique terminal keyed return covering every scout-bound scalar KEY_VALUE path and
    every TABLE_COLUMN path as list-of-record reads via the shared ``_table_group_read_lines`` reader,
    seeding per-row None leaves for sibling declaration columns alongside None-default top-level
    declarations and the resolved download descriptor for the mixed shape.

    Return None when it does not own the sole terminal return (zero returns, or the single
    generator-owned download-idiom return), on any ungrounded or unbound required path, on nested-array
    paths, on an array declaration with no unique matching table group, and on any minted-name collision."""
    if download_required_paths and not expects_download:
        return None
    if not scalar_required_paths:
        return None
    if plan is None:
        return None
    stripped_code = textwrap.dedent(code).strip()
    if not stripped_code:
        return None
    try:
        tree = ast.parse(stripped_code)
    except SyntaxError:
        return None

    bindings_by_path = {binding.output_path: binding for binding in plan.live_reads}
    ordered_required_paths = sorted(scalar_required_paths)
    if any(path not in bindings_by_path for path in ordered_required_paths):
        return None

    scalar_paths = [path for path in ordered_required_paths if bindings_by_path[path].kind == LiveReadKind.KEY_VALUE]
    table_paths = [path for path in ordered_required_paths if bindings_by_path[path].kind == LiveReadKind.TABLE_COLUMN]
    if len(scalar_paths) + len(table_paths) != len(ordered_required_paths):
        return None

    table_groups: dict[tuple[str, int, tuple[tuple[str, bool], ...]], list[LiveReadBinding]] = {}
    for path in table_paths:
        binding = bindings_by_path[path]
        prefix = _array_prefix(binding)
        if not prefix or not binding.row_selector or binding.row_count <= 0:
            return None
        if any(is_array for _name, is_array in output_path_segments(binding.output_path)[len(prefix) :]):
            return None
        table_groups.setdefault((binding.row_selector, binding.row_count, prefix), []).append(binding)

    minted_vars = [f"{_ENVELOPE_SCALAR_VAR_BASE}_{index}" for index in range(len(scalar_paths))]
    minted_names = set(minted_vars)
    for group_index, ((_row_selector, row_count, _prefix), group_bindings) in enumerate(sorted(table_groups.items())):
        minted_names.add(f"{_ENVELOPE_RECORDS_VAR_BASE}_{group_index}")
        for row_index in range(row_count):
            for binding_index in range(len(group_bindings)):
                minted_names.add(f"{_ENVELOPE_CELL_VAR_BASE}_{group_index}_{row_index}_{binding_index}")
    existing_names = _bound_or_referenced_identifiers(tree)
    if minted_names & existing_names:
        return None

    returns = _snippet_scope_returns(tree.body)
    download_descriptor_key = ""
    download_descriptor_expr = ""
    if download_required_paths:
        if len(returns) != 1 or returns[0] not in tree.body:
            return None
        idiom_return = returns[0]
        if idiom_return.col_offset != 0:
            return None
        if not isinstance(idiom_return.value, ast.Dict) or len(idiom_return.value.keys) != 1:
            return None
        descriptor_key_node = idiom_return.value.keys[0]
        if not isinstance(descriptor_key_node, ast.Constant) or not isinstance(descriptor_key_node.value, str):
            return None
        descriptor_value = idiom_return.value.values[0]
        if not isinstance(descriptor_value, (ast.Name, ast.Attribute)):
            return None
        download_descriptor_key = descriptor_key_node.value
        download_descriptor_expr = ast.unparse(descriptor_value)
        preserved_lines = stripped_code.splitlines()[: idiom_return.lineno - 1]
    else:
        if returns:
            return None
        preserved_lines = stripped_code.splitlines()

    top_level_declarations: list[tuple[tuple[str, bool], ...]] = []
    group_none_leaves: dict[tuple[str, int, tuple[tuple[str, bool], ...]], list[tuple[tuple[str, bool], ...]]] = {}
    for path in sorted(declaration_paths):
        segments = output_path_segments(path)
        declaration_prefix = _array_prefix_of_segments(segments)
        if not declaration_prefix:
            top_level_declarations.append(segments)
            continue
        relative_segments = segments[len(declaration_prefix) :]
        if any(is_array for _name, is_array in relative_segments):
            return None
        matching_groups = [key for key in table_groups if key[2] == declaration_prefix]
        if len(matching_groups) != 1:
            return None
        group_none_leaves.setdefault(matching_groups[0], []).append(relative_segments)

    return_root = _ExtractionReturnNode()
    scalar_statements: list[str] = []
    for path, variable in zip(scalar_paths, minted_vars):
        scalar_statements.extend(_key_value_scalar_read_statements(bindings_by_path[path], variable, guard_empty=True))
        _set_return_expression(return_root, output_path_segments(path), variable)
    table_statements: list[str] = []
    for group_index, (group_key, group_bindings) in enumerate(sorted(table_groups.items())):
        row_selector, row_count, prefix = group_key
        records_variable = f"{_ENVELOPE_RECORDS_VAR_BASE}_{group_index}"
        table_statements.extend(
            _table_group_read_lines(
                group_bindings,
                row_selector=row_selector,
                row_count=row_count,
                prefix=prefix,
                group_index=group_index,
                records_variable=records_variable,
                cell_variable_base=_ENVELOPE_CELL_VAR_BASE,
                assemble_as_literal=True,
                guard_empty=True,
                none_leaf_segments=group_none_leaves.get(group_key, ()),
            )
        )
        _set_return_expression(return_root, prefix, records_variable)
    for segments in top_level_declarations:
        _set_return_expression(return_root, segments, "None")
    if download_descriptor_key:
        return_root.children.setdefault(
            download_descriptor_key, _ExtractionReturnNode()
        ).value_expression = download_descriptor_expr

    body_lines = list(preserved_lines)
    body_lines.extend(scalar_statements)
    body_lines.extend(table_statements)
    body_lines.append(f"return {_return_node_expression(return_root)}")
    produced_code = "\n".join(body_lines).strip() + "\n"
    keyed_paths = tuple(sorted(scalar_required_paths | declaration_paths))
    return ProducedStaticReturnEnvelope(code=produced_code, keyed_paths=keyed_paths)


def _trajectory_contains_reveal(trajectory: Sequence[Mapping[str, Any]], plan: RequestedOutputExtractionPlan) -> bool:
    return any(
        str(interaction.get("tool_name") or "") == "click"
        and (
            (bool(plan.reveal.selector) and str(interaction.get("selector") or "") == plan.reveal.selector)
            or (
                bool(plan.reveal.role and plan.reveal.name)
                and str(interaction.get("role") or "") == plan.reveal.role
                and str(interaction.get("accessible_name") or "") == plan.reveal.name
            )
        )
        for interaction in trajectory
    )


def synthesize_code_block_with_extraction(
    trajectory: Sequence[Mapping[str, Any]],
    extraction_plan: RequestedOutputExtractionPlan,
    *,
    strict_selectors: bool = False,
    reached_download_target: ReachedDownloadTarget | None = None,
    parameter_binding_snapshot: AuthoringParameterBindingSnapshot | None = None,
) -> SynthesizedCodeBlock | None:
    if not _trajectory_contains_reveal(trajectory, extraction_plan):
        return None
    interaction = synthesize_code_block(
        trajectory,
        strict_selectors=strict_selectors,
        reached_download_target=reached_download_target,
        parameter_binding_snapshot=parameter_binding_snapshot,
    )
    suffix = synthesize_extraction_suffix(extraction_plan)
    if interaction is None or suffix is None:
        return None
    interaction_code = interaction.code.rstrip() + "\n"
    interaction.code = interaction_code + suffix.code
    interaction.interaction_code = interaction_code
    interaction.extraction_code = suffix.code
    interaction.extraction_fingerprint = suffix.fingerprint
    interaction.extraction_plan_identity = extraction_plan.identity
    return interaction


def freeze_requested_output_extraction_candidate(
    synthesized: SynthesizedCodeBlock,
    plan: RequestedOutputExtractionPlan,
    *,
    source: str,
) -> FrozenRequestedOutputExtractionCandidate | None:
    if (
        not synthesized.extraction_code
        or not synthesized.extraction_fingerprint
        or synthesized.extraction_plan_identity != plan.identity
    ):
        return None
    return FrozenRequestedOutputExtractionCandidate(
        plan_identity=plan.identity,
        observation_identity=plan.observation_identity,
        requested_output_paths=plan.requested_output_paths,
        reveal=plan.reveal,
        interaction_code=synthesized.interaction_code,
        extraction_code=synthesized.extraction_code,
        source=source,
        admission_result="admitted",
        fingerprint=synthesized.extraction_fingerprint,
    )


# Model-owned slots the synthesizer cannot prove; the model fills these.
_FILL_DECLARED_GOAL = "<fill: the durable goal this block accomplishes>"
_FILL_CLAIM_ID = "claim:<fill>"
_FILL_CLAIM_TEXT = "<fill: the user-facing outcome this block claims>"
_FILL_CRITERION_ID = "criterion:<fill>"
_FILL_CRITERION_TEXT = "<fill: the terminal completion criterion>"
_FILL_EXTRACTION_SCHEMA = (
    "<fill: JSON Schema string of the fields to extract, e.g. "
    '{"type":"object","properties":{"field_a":{"type":"string"}},"required":["field_a"]}>'
)


def artifact_dependency_id(block_label: str) -> str:
    return f"dependency:{_slug(block_label)}_reached"


def artifact_observation_ref_id(block_label: str) -> str:
    return f"observation:{_slug(block_label)}_scout"


def build_artifact_metadata_skeleton(
    trajectory: Sequence[Mapping[str, Any]],
    *,
    block_label: str,
) -> dict[str, Any]:
    """Fill only the scout-proven evidence shape (`observed_not_verified`); the terminal goal and
    outcomes stay as `<fill>` placeholders the model owns."""
    dependency_id = artifact_dependency_id(block_label)
    observation_ref_id = artifact_observation_ref_id(block_label)
    # First recorded source_url is the page the synthesized block's leading `page.goto` lands on.
    # The trajectory carries only pre-action source pages, not post-action reached URLs, so this is
    # an advisory entry-page hint, never a reached-page identity (which SPAs would mis-key anyway).
    entry_url_hint = next(
        (url for url in (str(interaction.get("source_url") or "").strip() for interaction in trajectory) if url),
        None,
    )

    page_dependency: dict[str, Any] = {
        "id": dependency_id,
        "scope": "page",
        "status": "observed_not_verified",
        "observation_refs": [observation_ref_id],
    }
    if entry_url_hint:
        page_dependency["url_hint"] = _scrub_url_for_code_literal(entry_url_hint)

    observation_ref: dict[str, Any] = {
        "observation_ref": observation_ref_id,
        "dependency_id": dependency_id,
        "status": "observed_not_verified",
        "source_tool": SCOUT_INTERACTION_EVIDENCE_TOOL,
    }

    return {
        "block_label": block_label,
        "declared_goal": _FILL_DECLARED_GOAL,
        "page_dependencies": [page_dependency],
        "observation_refs": [observation_ref],
        "claimed_outcomes": [
            {
                "id": _FILL_CLAIM_ID,
                "scope": "outcome",
                "text": _FILL_CLAIM_TEXT,
                "status": "observed_not_verified",
                "depends_on": [dependency_id],
                "covered_criteria": [_FILL_CRITERION_ID],
                "goal_value_paths": ["<fill: output JSON path(s) carrying requested goal values>"],
                "extraction_schema": _FILL_EXTRACTION_SCHEMA,
                "extraction_schema_provenance": "self_authored",
                "observation_refs": [observation_ref_id],
            }
        ],
        "completion_criteria": [
            {
                "id": _FILL_CRITERION_ID,
                "text": _FILL_CRITERION_TEXT,
                "level": "terminal",
                "terminal": True,
                "judgment_predicate": None,
                "judgment_polarity_when_holds": None,
            }
        ],
        "terminal_verifier_expectations": [
            {
                "id": "expectation:<fill>",
                "text": "<fill: what terminal verification must observe>",
                "criteria_ids": [_FILL_CRITERION_ID],
                "goal_value_paths": ["<fill: output JSON path(s) carrying requested goal values>"],
            }
        ],
    }


def code_contains_credential_fill(code: str) -> bool:
    return CREDENTIAL_FILL_CODE_PATTERN.search(code) is not None


def trajectory_has_credential_fill(trajectory: Sequence[Mapping[str, Any]]) -> bool:
    for interaction in trajectory:
        if str(interaction.get("tool_name") or "") != CREDENTIAL_FILL_TOOL_NAME:
            continue
        if str(interaction.get("credential_field") or "").strip() in _CREDENTIAL_FIELDS:
            return True
    return False


def trajectory_has_browser_fill_interaction(trajectory: Sequence[Mapping[str, Any]]) -> bool:
    for interaction in trajectory:
        tool_name = str(interaction.get("tool_name") or "")
        typed_length = interaction.get("typed_length")
        if tool_name == "type_text" and (
            (isinstance(typed_length, int) and typed_length > 0) or str(interaction.get("typed_value") or "").strip()
        ):
            return True
        if tool_name == "select_option" and str(interaction.get("value") or "").strip():
            return True
        if tool_name == CREDENTIAL_FILL_TOOL_NAME and str(interaction.get("credential_field") or "").strip():
            return True
    return False


def build_synthesized_artifact_metadata(trajectory: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return build_artifact_metadata_skeleton(trajectory, block_label=_SYNTHESIZED_BLOCK_LABEL)


def _render_artifact_metadata_block(metadata: Mapping[str, Any]) -> str:
    return json.dumps(metadata, indent=2, sort_keys=True)


def render_synthesized_offer_text(
    synthesized: SynthesizedCodeBlock,
    trajectory: Sequence[Mapping[str, Any]] | None = None,
    goal: str | None = None,
) -> str:
    """Render the offer body the copilot sees for a synthesized block (pure)."""
    param_keys = [p.get("key", "") for p in synthesized.parameters if p.get("key") and not p.get("credential_id")]
    credential_parameters = [p for p in synthesized.parameters if p.get("key") and p.get("credential_id")]
    extraction_instruction = (
        "The same interaction-reached evidence compiled the requested keyed extraction suffix; preserve both "
        "the reveal and extraction segments VERBATIM."
        if synthesized.extraction_code
        else (
            "Hand-author a contract-shaped keyed structure for any requested outputs, never a flat "
            "`page.inner_text(...)` / `text_content(...)` blob."
        )
    )
    parts = [
        "SYNTHESIZED CODE BLOCK (offered once). The page interactions you scouted were compiled into a "
        "deterministic Playwright snippet. Persist it VERBATIM as a `code` block labeled "
        f"`{_SYNTHESIZED_BLOCK_LABEL}` via update_workflow / update_and_run_blocks. {extraction_instruction}",
        "```python",
        synthesized.code.rstrip("\n"),
        "```",
    ]
    if synthesized.extraction_plan_identity:
        parts.append(f"Extraction plan identity: `{synthesized.extraction_plan_identity}`.")
    if param_keys:
        default_keys = [
            str(p.get("key") or "")
            for p in synthesized.parameters
            if p.get("key") and not p.get("credential_id") and p.get("default_value")
        ]
        line = "Workflow parameters referenced (bind these): " + ", ".join(param_keys) + "."
        line += " Bind each as `workflow_parameter_type: string`."
        if default_keys:
            line += " Server-side `default_value` is available for: " + ", ".join(default_keys) + "."
        parts.append(line)
    if credential_parameters:
        bindings = ", ".join(f"`{p['key']}` -> `{p['credential_id']}`" for p in credential_parameters)
        parts.append(
            "Credential parameters referenced: "
            + bindings
            + ". Bind each as a workflow parameter with `workflow_parameter_type: credential_id` and the "
            "credential ID in `default_value`; at runtime the key resolves to a credential object whose "
            "`.username` / `.password` attributes and `.otp()` method the snippet reads (`.otp()` resolves a "
            "fresh authenticator, email, or SMS one-time code during the run). Never replace these reads with "
            "literal values."
        )
    if "page.expect_download()" in synthesized.code:
        parts.append(
            "The snippet already fires the browser download (`async with page.expect_download()`) to the "
            "workflow output surface (`downloaded_files`). Do not re-fetch the file or place its bytes or URL "
            "in the reply; the data-capture step only needs to name the downloaded artifact, not refetch it."
        )
    if synthesized.notes:
        parts.append("Synthesis notes: " + "; ".join(synthesized.notes) + ".")
    if synthesized.steps:
        # escape_quotes + whitespace collapse keep the goal inside its quoted span in the prompt.
        goal_text = " ".join(escape_code_fences(goal or "", escape_quotes=True).split())
        goal_part = f' and set the block\'s `prompt` field to "{goal_text}"' if goal_text else ""
        parts.append(
            "On the same block, set the `steps` field to the JSON list below VERBATIM (plain-language "
            f"annotations mapping each step to the code lines it covers){goal_part}."
        )
        parts.append("```json")
        parts.append(json.dumps(synthesized.steps, indent=2, sort_keys=True))
        parts.append("```")
    if trajectory:
        metadata = build_synthesized_artifact_metadata(trajectory)
        parts.append(
            "Pass this `code_artifact_metadata` for the block (the scouted page evidence is filled in; "
            "replace the `<fill: ...>` slots with the terminal goal and outcome this block delivers, then "
            "include `goal_value_paths` for the output JSON fields carrying requested goal values, then "
            "submit it whole — the validator returns every remaining violation at once):"
        )
        parts.append("```json")
        parts.append(_render_artifact_metadata_block(metadata))
        parts.append("```")
        parts.append(
            "`extraction_schema` is the typed shape (named fields + types) of what this block extracts. "
            "Propose it from the goal and the page text you scouted, surface the proposed fields to the user, "
            "and ASK_QUESTION to confirm or adjust which fields to grab before committing the block. Carry the "
            "confirmed JSON Schema back as `extraction_schema`; `goal_value_paths` index into it. Shape the "
            "data-capture `return` so it conforms: each schema field is a named scalar key. For an array schema "
            "return an array of objects; for a single record return a keyed dict. Bind each `<fill: ...>` to the "
            "page text you captured. For example:"
        )
        parts.append("```python")
        parts.append('return {"records": [{"field_a": "...", "field_b": "..."}]}')
        parts.append("```")
    return "\n".join(parts)
