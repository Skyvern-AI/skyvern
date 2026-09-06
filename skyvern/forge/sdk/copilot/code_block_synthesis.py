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
    SameMonthFileMatchTransform,
    authoring_parameter_binding_fingerprint,
    same_month_file_match_transform_fingerprint,
    same_month_file_match_transform_is_valid,
)
from skyvern.forge.sdk.copilot.credential_fill_fields import CREDENTIAL_FILL_FIELDS
from skyvern.forge.sdk.copilot.output_extraction_plan import output_path_segments
from skyvern.forge.sdk.copilot.reached_download_target import (
    DOWNLOAD_CLAIM_HELPER_NAME,
    ReachedDownloadTarget,
    can_deliver_registered_download,
)
from skyvern.forge.sdk.copilot.runtime import (
    ScoutedDynamicRowEvidence,
    ScoutedDynamicRowPeriodMatch,
)

LOG = structlog.get_logger()

_MAX_STEPS = 60
_INDENT = "    "
# A dashboard renders the tile before the figure it will hold, so a designated read waits for the
# value rather than reporting the empty frame it lands in first.
_DOMCONTENTLOADED = "domcontentloaded"
_ENTRY_TARGET_VAR = "_scout_entry_target"
_DOWNLOAD_TARGET_VAR = "_scout_download_target"
_SAME_MONTH_HELPER_VAR = "_scout_same_month_iso"
_ENTRY_REUSED_VAR = "_scout_entry_reused_current_page"
_ENTRY_RESUME_AFTER_AUTH_VAR = "_scout_entry_resume_after_auth"
_ENTRY_RESUME_TARGET_VAR = "_scout_entry_resume_target"
_ENTRY_OPENER_VAR = "_scout_entry_opener"
_OPTIONAL_DISMISSAL_VAR = "_scout_optional_dismissal"
_READONLY_DEFERRED_VAR = "_scout_readonly_actual"
_MONTH_HELPER_VAR = "_scout_month_to_iso"
_ISO_DATE_HELPER_VAR = "_scout_iso_date_to_year_month"
_PERIOD_DATE_PATTERN_HELPER_VAR = "_scout_period_date_pattern"
_INTERNAL_SCOUT_VARS = (
    _ENTRY_TARGET_VAR,
    _DOWNLOAD_TARGET_VAR,
    _ENTRY_REUSED_VAR,
    _ENTRY_RESUME_AFTER_AUTH_VAR,
    _ENTRY_RESUME_TARGET_VAR,
    _ENTRY_OPENER_VAR,
    _OPTIONAL_DISMISSAL_VAR,
    _READONLY_DEFERRED_VAR,
    _MONTH_HELPER_VAR,
    _SAME_MONTH_HELPER_VAR,
    _ISO_DATE_HELPER_VAR,
    _PERIOD_DATE_PATTERN_HELPER_VAR,
)

# Base name for the download var bound by `async with page.expect_download() as <name>:`.
_DOWNLOAD_VAR_BASE = "dl_info"
_DOWNLOAD_FILENAME_VAR_BASE = "downloaded_file_name"
_DOWNLOAD_PATH_VAR_BASE = "_downloaded_file_path"
_DOWNLOAD_OUTPUT_VAR_BASE = "downloaded_files"

CREDENTIAL_FILL_TOOL_NAME = "fill_credential_field"
_CREDENTIAL_FIELDS = CREDENTIAL_FILL_FIELDS

# Shape of a synthesized credential fill, ``.fill(<param>.<field>)`` or the runtime OTP
# accessor ``.fill(await <param>.otp())`` — distinguishes a login fill from a plain
# ``.fill(str(<key>))`` text input.
CREDENTIAL_FILL_CODE_PATTERN = re.compile(r"\.fill\(\s*(?:[A-Za-z_]\w*\.\w+|await\s+[A-Za-z_]\w*\.otp\(\))\s*\)")
# Credential fields the scout must fill live before a code block reading them may persist;
# `.otp()` resolves at runtime only, so totp never requires (or credits) a live scout fill.
ONE_TIME_CODE_CREDENTIAL_FIELD = "totp"


def credential_fill_source(locator_expr: str, param_key: str, field: str) -> str:
    if field == ONE_TIME_CODE_CREDENTIAL_FIELD:
        return f"await {locator_expr}.fill(await {param_key}.otp())"
    return f"await {locator_expr}.fill({param_key}.{field})"


def wrapped_code_ast(code: str) -> ast.AST | None:
    body = "\n".join(f"    {line}" for line in code.splitlines())
    if not body.strip():
        body = "    pass"
    try:
        return ast.parse(f"async def __submitted_code__():\n{body}\n")
    except SyntaxError:
        return None


def _is_submit_interaction(interaction: Mapping[str, Any]) -> bool:
    """A submit is a click, or an Enter keypress; other keys (Tab between fields) are not submits."""
    tool_name = str(interaction.get("tool_name") or "").strip()
    if tool_name == "click":
        return True
    return tool_name == "press_key" and str(interaction.get("key") or "").strip() == "Enter"


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


_LOGIN_SUBMIT_NAME_PATTERN = re.compile(
    r"^(?:log in|login|sign in|authenticate)(?: now| securely| to continue)?$",
    re.I,
)
_LOGIN_SUBMIT_SELECTOR_PATTERN = re.compile(
    r"^(?:(?:log in|login|sign in|authenticate)(?: submit| button| btn)?|"
    r"(?:submit|button|btn) (?:log in|login|sign in|authenticate))$",
    re.I,
)


def last_scout_credential_fill_index(trajectory: Sequence[Any]) -> int | None:
    # Boundary past the ENTIRE credential flow, including a runtime-only OTP/MFA fill. Keying only on
    # username/password let an MFA step (fill totp -> verify-click) form a durable entry->commit past
    # the boundary and falsely release the terminal-action gate on a login-only trajectory.
    last_index: int | None = None
    for index, item in enumerate(trajectory):
        if isinstance(item, Mapping) and str(item.get("tool_name") or "").strip() == CREDENTIAL_FILL_TOOL_NAME:
            last_index = index
    return last_index


def first_stable_login_submit_index(interactions: Sequence[Mapping[str, Any]], credential_index: int) -> int | None:
    for index, interaction in enumerate(interactions[credential_index + 1 :], start=credential_index + 1):
        tool_name = str(interaction.get("tool_name") or "").strip()
        if tool_name == "press_key" and str(interaction.get("key") or "").strip() == "Enter":
            return index
        if tool_name != "click":
            continue
        accessible_name = re.sub(r"[^a-z0-9]+", " ", str(interaction.get("accessible_name") or "").lower()).strip()
        selector = re.sub(r"[^a-z0-9]+", " ", str(interaction.get("selector") or "").lower()).strip()
        if _LOGIN_SUBMIT_NAME_PATTERN.fullmatch(accessible_name) or _LOGIN_SUBMIT_SELECTOR_PATTERN.fullmatch(selector):
            return index
    return None


def credential_submit_boundary_index(interactions: Sequence[Mapping[str, Any]], credential_index: int) -> int | None:
    """The submit that commits the scout's login: a stable login-submit identity, else the first submit
    after the latest credential fill on that fill's own page. None when neither identifies one."""
    submit_index = first_stable_login_submit_index(interactions, credential_index)
    if submit_index is not None:
        return submit_index
    latest_fill_source_url = str(interactions[credential_index].get("source_url") or "").strip()
    if not latest_fill_source_url:
        return None
    return first_matched_post_fill_submit_index(interactions, credential_index, {latest_fill_source_url})


def credential_segment_bounds(trajectory: Sequence[Mapping[str, Any]]) -> list[tuple[int, int]] | None:
    """Inclusive trajectory bounds for each durable segment of a credentialed scout: the login flow up
    to its submit, the business steps that follow, and the value read. None when the trajectory carries
    no credential fill or no identifiable submit, which leaves the single-block shape in effect."""
    fill_index = last_scout_credential_fill_index(trajectory)
    if fill_index is None:
        return None
    submit_index = credential_submit_boundary_index(trajectory, fill_index)
    if submit_index is None:
        return None
    last_index = len(trajectory) - 1
    if submit_index >= last_index:
        return None
    first_read = next(
        (
            index
            for index in range(submit_index + 1, len(trajectory))
            if str(trajectory[index].get("tool_name") or "") == "read_value"
        ),
        None,
    )
    bounds = [(0, submit_index)]
    if first_read is None:
        bounds.append((submit_index + 1, last_index))
    else:
        if first_read > submit_index + 1:
            bounds.append((submit_index + 1, first_read - 1))
        bounds.append((first_read, last_index))
    return bounds


def _captcha_boundary_indices(trajectory: Sequence[Mapping[str, Any]]) -> set[int]:
    """Return credential-associated submit boundaries."""
    boundaries: set[int] = set()
    latest_credential_fill_by_source: dict[str, int] = {}
    for index, interaction in enumerate(trajectory):
        if str(interaction.get("tool_name") or "") != CREDENTIAL_FILL_TOOL_NAME:
            continue
        if str(interaction.get("credential_field") or "").strip() not in _CREDENTIAL_FIELDS:
            continue
        source_url = str(interaction.get("source_url") or "").strip()
        if source_url:
            latest_credential_fill_by_source[source_url] = index
    for source_url, latest_fill_index in latest_credential_fill_by_source.items():
        submit_index = first_matched_post_fill_submit_index(
            trajectory,
            latest_fill_index,
            frozenset({source_url}),
        )
        if submit_index is not None:
            boundaries.add(submit_index)
    return boundaries


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

# Ceiling for a wait the scout proved must succeed (entry target, replayed read, extraction
# container). A wait returns the moment its condition holds, so a fast page pays nothing; only a
# genuinely absent state pays the full budget. Distinct from the deliberate 1s speculative probes.
_REQUIRED_STATE_TIMEOUT_MS = 120_000

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
        "solve_captcha",
        "search_web",
        DOWNLOAD_CLAIM_HELPER_NAME,
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
        _ISO_DATE_HELPER_VAR,
        _PERIOD_DATE_PATTERN_HELPER_VAR,
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

# Root containers every document already has, so a readiness wait on one encodes no precondition:
# it either passes immediately or burns its whole timeout with the container already resolved.
_ROOT_LOCATOR_SELECTORS = frozenset({"body", "html", ":root", "*"})


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
    # May contain bounded source-row text for in-memory fingerprint validation. The only public boundary
    # is `_public_locator_provenance`, which emits origin/input metadata and omits the captured text.
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
    # Durable segments of a credentialed trajectory (login / business / read), each synthesized from
    # its own slice so it is self-contained and independently runnable. Empty when the trajectory has
    # no credential boundary, which leaves the single-block shape in effect.
    segments: list[SynthesizedCodeBlock] = field(default_factory=list)
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


def is_root_locator_selector(selector: str) -> bool:
    return selector.strip().casefold() in _ROOT_LOCATOR_SELECTORS


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
SAME_MONTH_FILE_MATCH_PROVENANCE_SOURCE = "same_month_file_match"
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
_PERIOD_DAY_PATTERN_BY_MAX = {
    28: r"(?:0?[1-9]|1[0-9]|2[0-8])",
    29: r"(?:0?[1-9]|1[0-9]|2[0-9])",
    30: r"(?:0?[1-9]|[12][0-9]|30)",
    31: r"(?:0?[1-9]|[12][0-9]|3[01])",
}
# Intentionally scoped to the English "Month D, YYYY" labels this grounded route can witness.
# Additional formats must preserve Python/browser parity instead of adding website-specific parsing.
_ROW_PERIOD_DATE_RE = re.compile(
    r"\b(" + "|".join(_WITNESS_MONTH_TO_ISO) + r")\s+(0?[1-9]|[12][0-9]|3[01]),\s+([0-9]{4})\b",
    re.IGNORECASE,
)


class _InputTemplatingPlan(NamedTuple):
    surface: str
    selector: str
    role: str
    name: str
    holes: list[Mapping[str, Any]]
    dynamic_row_evidence: ScoutedDynamicRowEvidence | None


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


def _days_in_month(year: int, month: int) -> int:
    if month == 2:
        return 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28
    return 30 if month in (4, 6, 9, 11) else 31


def _iso_date_to_year_month(value: str) -> str | None:
    parts = value.split("-")
    if len(parts) != 3:
        return None
    year_text, month_text, day_text = parts
    if (
        len(year_text) != 4
        or len(month_text) != 2
        or len(day_text) != 2
        or not year_text.isdigit()
        or not month_text.isdigit()
        or not day_text.isdigit()
    ):
        return None
    year, month, day = int(year_text), int(month_text), int(day_text)
    if year < 1 or month < 1 or month > 12 or day < 1 or day > _days_in_month(year, month):
        return None
    return f"{year_text}-{month_text}"


def _witness_observed_forms(value: str) -> list[tuple[str, str]]:
    forms: list[tuple[str, str]] = [("identity", value)]
    iso = _month_name_to_iso(value)
    if iso is not None and iso != value:
        forms.append(("month_name_to_iso", iso))
    year_month = _iso_date_to_year_month(value)
    if year_month is not None and year_month != value:
        forms.append(("iso_date_to_year_month", year_month))
    return forms


def _row_period_tokens(row_text: str) -> list[tuple[str, int]]:
    normalized = " ".join(row_text.split())
    periods: list[tuple[str, int]] = []
    for match in _ROW_PERIOD_DATE_RE.finditer(normalized):
        month_name = match.group(1).lower()
        month = _WITNESS_MONTH_TO_ISO.get(month_name)
        if month is None:
            continue
        day = int(match.group(2))
        year = match.group(3)
        year_number = int(year)
        if year_number < 1 or day < 1 or day > _days_in_month(year_number, int(month)):
            continue
        periods.append((f"{year}-{month}", match.start()))
    return periods


def _strict_period_date_pattern(period: str) -> re.Pattern[str] | None:
    parts = period.split("-")
    if (
        len(parts) != 2
        or len(parts[0]) != 4
        or not parts[0].isdigit()
        or int(parts[0]) < 1
        or len(parts[1]) != 2
        or not parts[1].isdigit()
        or not 1 <= int(parts[1]) <= 12
    ):
        return None
    month_names = tuple(name.title() for name in _WITNESS_MONTH_TO_ISO)
    max_day = _days_in_month(int(parts[0]), int(parts[1]))
    day = _PERIOD_DAY_PATTERN_BY_MAX[max_day]
    return re.compile(
        rf"\b{re.escape(month_names[int(parts[1]) - 1])}\s+{day},\s+{re.escape(parts[0])}\b",
        re.IGNORECASE,
    )


def validated_dynamic_row_period_matches(
    value: Any, row_selector_count: int
) -> list[ScoutedDynamicRowPeriodMatch] | None:
    if not isinstance(value, list) or len(value) > 20:
        return None
    result: list[ScoutedDynamicRowPeriodMatch] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"period", "selected_row_match_count", "row_match_count"}:
            return None
        period = item.get("period")
        selected_count = item.get("selected_row_match_count")
        row_count = item.get("row_match_count")
        if (
            not isinstance(period, str)
            or _strict_period_date_pattern(period) is None
            or isinstance(selected_count, bool)
            or not isinstance(selected_count, int)
            or selected_count < 1
            or selected_count > 20
            or isinstance(row_count, bool)
            or not isinstance(row_count, int)
            or row_count < 1
            or row_count > row_selector_count
        ):
            return None
        result.append(
            ScoutedDynamicRowPeriodMatch(
                period=period,
                selected_row_match_count=selected_count,
                row_match_count=row_count,
            )
        )
    if [item["period"] for item in result] != sorted({str(item["period"]) for item in result}):
        return None
    return result


def dynamic_row_period_matches_match_selected_row(row_text: str, period_matches: Sequence[Mapping[str, Any]]) -> bool:
    selected_counts: dict[str, int] = {}
    for period, _ in _row_period_tokens(row_text):
        selected_counts[period] = selected_counts.get(period, 0) + 1
    return selected_counts == {str(item["period"]): int(item["selected_row_match_count"]) for item in period_matches}


def dynamic_row_evidence_fingerprint(
    *,
    source_url: str,
    target_selector: str,
    row_selector: str,
    row_text: str,
    row_selector_count: int,
    row_text_match_count: int,
    period_matches: Sequence[Mapping[str, Any]],
    selected_index: int,
) -> str:
    payload = {
        "source_url": source_url,
        "target_selector": target_selector,
        "row_selector": row_selector,
        "row_text": row_text,
        "row_selector_count": row_selector_count,
        "row_text_match_count": row_text_match_count,
        "period_matches": [dict(item) for item in period_matches],
        "selected_index": selected_index,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _validated_dynamic_row_evidence(interaction: Mapping[str, Any]) -> ScoutedDynamicRowEvidence | None:
    evidence = interaction.get("dynamic_row_evidence")
    if not isinstance(evidence, Mapping):
        return None
    source_url = str(interaction.get("source_url") or "").strip()
    selector = str(interaction.get("selector") or "").strip()
    row_source_url = evidence.get("source_url")
    target_selector = evidence.get("target_selector")
    row_selector = evidence.get("row_selector")
    row_text = evidence.get("row_text")
    row_selector_count = evidence.get("row_selector_count")
    row_text_match_count = evidence.get("row_text_match_count")
    period_matches = (
        validated_dynamic_row_period_matches(evidence.get("period_matches"), row_selector_count)
        if isinstance(row_selector_count, int) and not isinstance(row_selector_count, bool)
        else None
    )
    selected_index = evidence.get("selected_index")
    evidence_fingerprint = evidence.get("evidence_fingerprint")
    if (
        not source_url
        or row_source_url != source_url
        or target_selector != selector
        or not isinstance(row_selector, str)
        or not row_selector.strip()
        or _is_positional_selector(row_selector)
        or _is_bare_ambiguous_selector(row_selector)
        or not isinstance(row_text, str)
        or not row_text.strip()
        or len(row_text) > 500
        or isinstance(row_selector_count, bool)
        or not isinstance(row_selector_count, int)
        or row_selector_count < 2
        or row_selector_count > 100
        or isinstance(row_text_match_count, bool)
        or not isinstance(row_text_match_count, int)
        or row_text_match_count < 1
        or row_text_match_count > row_selector_count
        or period_matches is None
        or not dynamic_row_period_matches_match_selected_row(" ".join(row_text.split()), period_matches)
        or isinstance(selected_index, bool)
        or not isinstance(selected_index, int)
        or selected_index < 0
        or selected_index >= row_selector_count
        or not isinstance(evidence_fingerprint, str)
        or evidence_fingerprint
        != dynamic_row_evidence_fingerprint(
            source_url=source_url,
            target_selector=selector,
            row_selector=row_selector.strip(),
            row_text=" ".join(row_text.split()),
            row_selector_count=row_selector_count,
            row_text_match_count=row_text_match_count,
            period_matches=period_matches,
            selected_index=selected_index,
        )
    ):
        return None
    return ScoutedDynamicRowEvidence(
        source_url=source_url,
        target_selector=selector,
        row_selector=row_selector.strip(),
        row_text=" ".join(row_text.split()),
        row_selector_count=row_selector_count,
        row_text_match_count=row_text_match_count,
        period_matches=period_matches,
        selected_index=selected_index,
        evidence_fingerprint=evidence_fingerprint,
    )


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
        expression = _witness_transform_expression(key, str(hole.get("transform") or "identity"))
        if expression is None:
            return None
        segments.append("{" + expression + "}")
        cursor = idx + len(matched_literal)
    segments.append(_escape_fstring_literal_segment(raw[cursor:]))
    return "".join(segments)


def _witness_transform_expression(key: str, transform: str) -> str | None:
    if transform == "identity":
        return key
    if transform == "month_name_to_iso":
        return f"{_SCOUT_MONTH_HELPER_NAME}({key})"
    if transform == "iso_date_to_year_month":
        return f"{_ISO_DATE_HELPER_VAR}({key})"
    return None


def _input_templated_holes_are_self_validating(holes: Sequence[Mapping[str, Any]]) -> bool:
    for hole in holes:
        matched_literal = str(hole.get("matched_literal") or "")
        inputs = _correspondence_inputs(hole)
        keys = [witness["input_key"] for witness in inputs]
        if not matched_literal or not keys or keys != sorted(set(keys)):
            return False
        for witness in inputs:
            if not _witness_key_is_safe(witness["input_key"]):
                return False
            forms = _witness_observed_forms(witness["parameter_value"])
            if (witness["transform"], matched_literal) not in forms:
                return False
    return True


def build_input_templated_locator(
    *,
    surface: str,
    selector: str,
    role: str,
    name: str,
    holes: Sequence[Mapping[str, Any]],
    row_text: str = "",
    period_matches: Sequence[Mapping[str, Any]] = (),
) -> str | None:
    """Single source for the templated locator literal, used at emission AND re-derived byte-for-byte at
    the admissibility seam so a tampered or reordered provenance record fails the recompute equality check."""
    if not holes or not _input_templated_holes_are_self_validating(holes):
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
    if surface == "row_text":
        if len(holes) != 1 or not selector or not row_text:
            return None
        hole = holes[0]
        matched_literal = str(hole.get("matched_literal") or "")
        if (
            len([period for period, _ in _row_period_tokens(row_text) if period == matched_literal]) != 1
            or _strict_period_date_pattern(matched_literal) is None
            or not any(
                item.get("period") == matched_literal
                and item.get("selected_row_match_count") == 1
                and item.get("row_match_count") == 1
                for item in period_matches
            )
        ):
            return None
        key = str(hole.get("input_key") or "")
        transformed = _witness_transform_expression(key, str(hole.get("transform") or "identity"))
        if transformed is None:
            return None
        return f"page.locator({_py_str(selector)}).filter(has_text={_PERIOD_DATE_PATTERN_HELPER_VAR}({transformed}))"
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
        surface=plan.surface,
        selector=plan.selector,
        role=plan.role,
        name=plan.name,
        holes=plan.holes,
        row_text=plan.dynamic_row_evidence["row_text"] if plan.dynamic_row_evidence is not None else "",
        period_matches=plan.dynamic_row_evidence["period_matches"] if plan.dynamic_row_evidence is not None else (),
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
    row_holes = [c for c in correspondences if isinstance(c, Mapping) and c.get("surface") == "row_text"]
    parsed = _parse_role_name(selector) if selector else None
    dynamic_row = _validated_dynamic_row_evidence(interaction)
    if row_holes and dynamic_row is not None and len(row_holes) == 1:
        return _InputTemplatingPlan(
            surface="row_text",
            selector=dynamic_row["row_selector"],
            role="",
            name="",
            holes=row_holes,
            dynamic_row_evidence=dynamic_row,
        )
    if (
        selector_holes
        and selector
        and parsed is None
        and not _is_positional_selector(selector)
        and not _is_bare_ambiguous_selector(selector)
    ):
        ordered = _ordered_holes(selector, selector_holes)
        if ordered is not None:
            return _InputTemplatingPlan(
                surface="selector",
                selector=selector,
                role="",
                name="",
                holes=ordered,
                dynamic_row_evidence=None,
            )
    if name_holes and role and name:
        ambiguous_role = parsed is not None and not parsed[1]
        if not selector or _is_bare_ambiguous_selector(selector) or ambiguous_role:
            ordered = _ordered_holes(name, name_holes)
            if ordered is not None:
                return _InputTemplatingPlan(
                    surface="accessible_name",
                    selector="",
                    role=role,
                    name=name,
                    holes=ordered,
                    dynamic_row_evidence=None,
                )
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
        surface=plan.surface,
        selector=plan.selector,
        role=plan.role,
        name=plan.name,
        holes=plan.holes,
        row_text=plan.dynamic_row_evidence["row_text"] if plan.dynamic_row_evidence is not None else "",
        period_matches=plan.dynamic_row_evidence["period_matches"] if plan.dynamic_row_evidence is not None else (),
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
                    **(
                        {"equivalent_inputs": [dict(equivalent) for equivalent in hole["equivalent_inputs"]]}
                        if isinstance(hole.get("equivalent_inputs"), list)
                        else {}
                    ),
                }
                for hole in plan.holes
            ],
        }
        if plan.surface == "selector":
            record["selector"] = plan.selector
        elif plan.surface == "accessible_name":
            record["role"] = plan.role
            record["name"] = plan.name
        elif plan.dynamic_row_evidence is not None:
            record.update(plan.dynamic_row_evidence)
        diagnostics.locator_provenance.append(record)
    return expr


def _correspondence_inputs(hole: Mapping[str, Any]) -> list[dict[str, str]]:
    inputs = [
        {
            "input_key": str(hole.get("input_key") or ""),
            "parameter_value": str(hole.get("parameter_value") or ""),
            "transform": str(hole.get("transform") or "identity"),
        }
    ]
    equivalents = hole.get("equivalent_inputs")
    if isinstance(equivalents, list):
        for equivalent in equivalents:
            if not isinstance(equivalent, Mapping):
                continue
            inputs.append(
                {
                    "input_key": str(equivalent.get("input_key") or ""),
                    "parameter_value": str(equivalent.get("parameter_value") or ""),
                    "transform": str(equivalent.get("transform") or "identity"),
                }
            )
    return inputs


def _prescan_input_templating(
    trajectory: Sequence[Mapping[str, Any]],
) -> tuple[list[str], bool, bool, bool, list[list[dict[str, str]]]]:
    keys: list[str] = []
    needs_month = False
    needs_iso_date = False
    needs_period_helpers = False
    collision_groups: list[list[dict[str, str]]] = []
    for interaction in trajectory:
        plan = _input_templating_plan(interaction)
        if plan is None:
            continue
        if plan.surface == "row_text":
            needs_period_helpers = True
        for hole in plan.holes:
            inputs = _correspondence_inputs(hole)
            if len(inputs) > 1:
                collision_groups.append(inputs)
            for witness in inputs:
                key = witness["input_key"]
                if key and key not in keys:
                    keys.append(key)
                if witness["transform"] == "month_name_to_iso":
                    needs_month = True
                elif witness["transform"] == "iso_date_to_year_month":
                    needs_iso_date = True
    return keys, needs_month, needs_iso_date, needs_period_helpers, collision_groups


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


def _scout_iso_date_helper_lines() -> list[str]:
    return [
        f"{_INDENT}def {_ISO_DATE_HELPER_VAR}(_value):",
        f"{_INDENT * 2}_parts = str(_value).split('-')",
        f"{_INDENT * 2}if not (len(_parts) == 3 and len(_parts[0]) == 4 and len(_parts[1]) == 2 "
        f"and len(_parts[2]) == 2 and all(_part.isdigit() for _part in _parts)):",
        f'{_INDENT * 3}raise Exception("unrecognized ISO date for grounded parameter")',
        f"{_INDENT * 2}_year, _month, _day = int(_parts[0]), int(_parts[1]), int(_parts[2])",
        f"{_INDENT * 2}_leap = _year % 4 == 0 and (_year % 100 != 0 or _year % 400 == 0)",
        f"{_INDENT * 2}_days = (31, 29 if _leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)",
        f"{_INDENT * 2}if _year < 1 or _month < 1 or _month > 12 or _day < 1 or _day > _days[_month - 1]:",
        f'{_INDENT * 3}raise Exception("unrecognized ISO date for grounded parameter")',
        f"{_INDENT * 2}return _parts[0] + '-' + _parts[1]",
    ]


def _scout_period_helper_lines() -> list[str]:
    month_names = "(" + ", ".join(f'"{name.title()}"' for name in _WITNESS_MONTH_TO_ISO) + ")"
    day_patterns = repr(_PERIOD_DAY_PATTERN_BY_MAX)
    return [
        f"{_INDENT}def {_PERIOD_DATE_PATTERN_HELPER_VAR}(_period):",
        f"{_INDENT * 2}_parts = str(_period).split('-')",
        f"{_INDENT * 2}if not (len(_parts) == 2 and len(_parts[0]) == 4 and _parts[0].isdigit() "
        f"and len(_parts[1]) == 2 and _parts[1].isdigit() and int(_parts[0]) >= 1 "
        f"and 1 <= int(_parts[1]) <= 12):",
        f'{_INDENT * 3}raise Exception("unrecognized grounded period")',
        f"{_INDENT * 2}_months = {month_names}",
        f"{_INDENT * 2}_year, _month = int(_parts[0]), int(_parts[1])",
        f"{_INDENT * 2}_leap = _year % 4 == 0 and (_year % 100 != 0 or _year % 400 == 0)",
        f"{_INDENT * 2}_max_day = (31, 29 if _leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)[_month - 1]",
        f"{_INDENT * 2}_day = {day_patterns}[_max_day]",
        f'{_INDENT * 2}return re.compile(r"\\b" + re.escape(_months[int(_parts[1]) - 1]) + r"\\s+" '
        f'+ _day + r",\\s+" + re.escape(_parts[0]) + r"\\b", re.IGNORECASE)',
    ]


def _witness_charset_guard_lines(key: str) -> list[str]:
    return [
        f"{_INDENT}if not (isinstance({key}, str) and {key} == {key}.strip() and {key}[:1].isalnum() "
        f'and all(_c.isalnum() or _c in " ._-" for _c in {key})):',
        f"{_INDENT * 2}raise Exception({_py_str(f'invalid value for grounded parameter {key}')})",
    ]


def witness_prelude_lines(
    keys: Sequence[str],
    *,
    include_month_helper: bool,
    include_iso_date_helper: bool = False,
    include_period_helpers: bool = False,
    collision_groups: Sequence[Sequence[Mapping[str, str]]] = (),
) -> list[str]:
    """Top-of-body guards (fail closed before any interpolation) plus the reserved month helper def.
    Reinjected into every separated browser stage because each stage is an independent CodeBlock."""
    lines: list[str] = []
    if include_month_helper:
        lines.extend(_scout_month_helper_lines())
    if include_iso_date_helper:
        lines.extend(_scout_iso_date_helper_lines())
    if include_period_helpers:
        lines.extend(_scout_period_helper_lines())
    for key in keys:
        lines.extend(_witness_charset_guard_lines(key))
    for group in collision_groups:
        transformed = [_witness_transform_expression(witness["input_key"], witness["transform"]) for witness in group]
        if not transformed or any(expression is None for expression in transformed):
            continue
        canonical = transformed[0]
        peers = transformed[1:]
        if not peers:
            continue
        comparison = " and ".join(f"{canonical} == {peer}" for peer in peers)
        lines.append(f"{_INDENT}if not ({comparison}):")
        lines.append(f'{_INDENT * 2}raise Exception("grounded parameters do not resolve to one period")')
    return lines


def _same_month_helper_lines() -> list[str]:
    return [
        f"{_INDENT}def {_SAME_MONTH_HELPER_VAR}(_start_value, _end_value):",
        f"{_INDENT * 2}def _parse_iso_date(_value):",
        f"{_INDENT * 3}_parts = str(_value).split({_py_str('-')})",
        f"{_INDENT * 3}if not (len(_parts) == 3 and len(_parts[0]) == 4 and len(_parts[1]) == 2 "
        f"and len(_parts[2]) == 2 and all(_part.isdigit() for _part in _parts)):",
        f'{_INDENT * 4}raise Exception("invalid full date for grounded file match")',
        f"{_INDENT * 3}_year = int(_parts[0])",
        f"{_INDENT * 3}_month = int(_parts[1])",
        f"{_INDENT * 3}_day = int(_parts[2])",
        f"{_INDENT * 3}if _year < 1 or _month < 1 or _month > 12:",
        f'{_INDENT * 4}raise Exception("invalid full date for grounded file match")',
        f"{_INDENT * 3}_leap = _year % 4 == 0 and (_year % 100 != 0 or _year % 400 == 0)",
        f"{_INDENT * 3}_days = (31, 29 if _leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)",
        f"{_INDENT * 3}if _day < 1 or _day > _days[_month - 1]:",
        f'{_INDENT * 4}raise Exception("invalid full date for grounded file match")',
        f"{_INDENT * 3}return (_year, _month, _day)",
        f"{_INDENT * 2}_start = _parse_iso_date(_start_value)",
        f"{_INDENT * 2}_end = _parse_iso_date(_end_value)",
        f"{_INDENT * 2}if _start[0] != _end[0] or _start[1] != _end[1]:",
        f'{_INDENT * 3}raise Exception("grounded file match dates must share one calendar month")',
        f"{_INDENT * 2}return str(_start[0]).zfill(4) + {_py_str('-')} + str(_start[1]).zfill(2)",
    ]


def build_same_month_file_match_selector(transform: SameMonthFileMatchTransform, selector: str) -> str | None:
    if (
        transform.selector != selector
        or transform.date_format_id != "iso_date_to_year_month"
        or not transform.holes
        or not same_month_file_match_transform_is_valid(transform)
    ):
        return None
    keys: set[str] = set()
    cursor = 0
    segments: list[str] = []
    date_holes = 0
    quoted_spans = _quoted_content_spans(selector)
    for hole in transform.holes:
        if (
            hole.position < cursor
            or not hole.matched_literal
            or selector[hole.position : hole.position + len(hole.matched_literal)] != hole.matched_literal
            or _boundary_delimited_positions(selector, hole.matched_literal, quoted_spans) != [hole.position]
            or not hole.declared_keys
            or any(not grounded_parameter_key_is_safe(key) or key in keys for key in hole.declared_keys)
        ):
            return None
        keys.update(hole.declared_keys)
        segments.append(_escape_fstring_literal_segment(selector[cursor : hole.position]))
        if hole.format_id == "identity" and len(hole.declared_keys) == 1:
            segments.append("{" + hole.declared_keys[0] + "}")
        elif hole.format_id == "iso_date_to_year_month" and hole.declared_keys == transform.date_keys:
            date_holes += 1
            segments.append(
                "{" + _SAME_MONTH_HELPER_VAR + "(" + transform.date_keys[0] + ", " + transform.date_keys[1] + ")}"
            )
        else:
            return None
        cursor = hole.position + len(hole.matched_literal)
    if date_holes != 1 or set(transform.date_keys) - keys:
        return None
    segments.append(_escape_fstring_literal_segment(selector[cursor:]))
    return 'f"' + "".join(segments) + '"'


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

    correspondences = interaction.get("input_correspondences")
    has_stamped_row_license = isinstance(correspondences, list) and any(
        isinstance(item, Mapping) and item.get("surface") == "row_text" for item in correspondences
    )

    if strict_selectors and (
        has_stamped_row_license
        or ("dynamic_row_evidence" in interaction and _validated_dynamic_row_evidence(interaction) is None)
    ):
        notes.append("dropped an interaction whose dynamic-row relation did not validate")
        if diagnostics is not None:
            diagnostics.dropped_interactions.append(
                {
                    "trajectory_index": trajectory_index if trajectory_index is not None else -1,
                    "tool_name": tool_name,
                    "selector": selector,
                    "reason_code": "invalid_dynamic_row_evidence",
                }
            )
        return ""

    if selector and is_root_locator_selector(selector):
        if role and name:
            return _get_by_role_expr_strict(role, name) if strict_selectors else _get_by_role_expr(role, name)
        notes.append(f"dropped an interaction targeting the root container {selector!r}")
        if diagnostics is not None:
            diagnostics.dropped_interactions.append(
                {
                    "trajectory_index": trajectory_index if trajectory_index is not None else -1,
                    "tool_name": tool_name,
                    "selector": selector,
                    "reason_code": "root_locator_target",
                }
            )
        return ""

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
    if diagnostics is not None:
        diagnostics.dropped_interactions.append(
            {
                "trajectory_index": trajectory_index if trajectory_index is not None else -1,
                "tool_name": tool_name,
                "reason_code": "missing_selector_and_role_name",
            }
        )
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


def _binds_block_output(interaction: Mapping[str, Any]) -> bool:
    if str(interaction.get("tool_name") or "") != "read_value":
        return False
    return bool(str(interaction.get("read_expression") or "").strip()) and str(
        interaction.get("read_output_path") or ""
    ).strip().startswith("output.")


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
    if len(prefix) == len(trajectory) or not prefix:
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
    file_match_transform: SameMonthFileMatchTransform | None = None,
    emit_read_return: bool = True,
    _segment_pass: bool = False,
) -> SynthesizedCodeBlock | None:
    """Deterministically synthesize a code block from a scout trajectory, or None if empty."""
    if not trajectory:
        return None
    # A named path keeps only its latest read: a re-read of the same requested value is a
    # refinement, including one that corrects a stale selector. The anonymous path is shared by every
    # read of an unnamed request, so there a different expression is an unrelated probe.
    latest_read_by_identity: dict[tuple[str, str], int] = {}
    for i, step in enumerate(trajectory):
        if str(step.get("tool_name") or "") == "read_value":
            path = str(step.get("read_output_path") or "")
            expression = str(step.get("read_expression") or "") if path == "output.scouted_read" else ""
            latest_read_by_identity[(path, expression)] = i
    if latest_read_by_identity:
        keep = set(latest_read_by_identity.values())
        trajectory = [
            step for i, step in enumerate(trajectory) if str(step.get("tool_name") or "") != "read_value" or i in keep
        ]

    lines: list[str] = []
    notes: list[str] = []
    parameters: list[dict[str, str]] = []
    diagnostics = SynthesisDiagnostics()
    steps: list[dict[str, Any]] = []
    read_bindings: list[tuple[str, str]] = []
    used_param_keys: set[str] = set()
    typed_param_keys: dict[tuple[str, str, str, str], str] = {}
    credential_param_keys: dict[str, str] = {}
    used_download_vars: set[str] = set()
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
    download_target_is_root = reached_download_target is not None and is_root_locator_selector(
        reached_download_target.selector
    )
    download_target_deliverable = (
        reached_download_target is not None
        and not reached_download_target.already_registered
        and bool(reached_download_target.selector)
        # A target the platform cannot register has no terminal to compile: emitting one would
        # spend the claim timeout to fail, where authoring and completion already agree it
        # cannot deliver.
        and can_deliver_registered_download(reached_download_target)
    )
    compile_download_target = download_target_deliverable and not download_target_is_root
    if download_target_is_root and download_target_deliverable and reached_download_target is not None:
        notes.append(
            f"dropped a download target on the root container {reached_download_target.selector!r}",
        )
        diagnostics.dropped_interactions.append(
            {
                "trajectory_index": -1,
                "tool_name": "download",
                "selector": reached_download_target.selector,
                "reason_code": "root_locator_target",
            }
        )
    file_match_locator = ""
    file_match_selector = ""
    file_match_keys: list[str] = []
    if file_match_transform is not None:
        if file_match_transform.provenance_fingerprint != same_month_file_match_transform_fingerprint(
            file_match_transform
        ):
            return None
        if not compile_download_target or reached_download_target is None:
            LOG.info(
                "copilot_spine_same_month_file_match_transform_dropped",
                reason_code="download_target_unavailable",
                selector_matches_transform=False,
            )
            file_match_transform = None
        else:
            file_match_selector = (
                build_same_month_file_match_selector(
                    file_match_transform,
                    reached_download_target.selector,
                )
                or ""
            )
            file_match_locator = f"page.locator({file_match_selector})" if file_match_selector else ""
            if not file_match_locator:
                LOG.info(
                    "copilot_spine_same_month_file_match_transform_dropped",
                    reason_code="locator_build_failed",
                    selector_matches_transform=file_match_transform.selector == reached_download_target.selector,
                )
                file_match_transform = None
        if file_match_transform is not None:
            for file_match_hole in file_match_transform.holes:
                for key in file_match_hole.declared_keys:
                    if key not in file_match_keys:
                        file_match_keys.append(key)
    if compile_download_target and reached_download_target is not None:
        trajectory, dropped_trailing = _trajectory_prefix_at_anchor(
            trajectory, reached_download_target.trajectory_anchor
        )
        if dropped_trailing:
            notes.append(f"dropped {dropped_trailing} interaction(s) captured after the download affordance")
            diagnostics.download_terminal_anchor = reached_download_target.trajectory_anchor
            diagnostics.download_terminal_dropped_trailing = dropped_trailing
            LOG.info(
                "copilot_spine_download_terminal_sequenced",
                anchor=reached_download_target.trajectory_anchor,
                dropped_trailing_count=dropped_trailing,
            )
    diagnostics.retained_trajectory_indices = list(range(len(trajectory)))

    (
        input_templated_keys,
        input_templated_needs_month,
        input_templated_needs_iso_date,
        input_templated_needs_period_helpers,
        input_templated_collision_groups,
    ) = _prescan_input_templating(trajectory)
    minted_input_witness_keys: set[str] = set()
    for interaction in trajectory:
        plan = _input_templating_plan(interaction)
        if plan is None:
            continue
        for hole in plan.holes:
            for witness in _correspondence_inputs(hole):
                key = witness["input_key"]
                if not key or key in minted_input_witness_keys:
                    continue
                minted_input_witness_keys.add(key)
                parameter = {"key": key, "source": LOCATOR_WITNESS_PARAM_SOURCE}
                # A file-match witness proves usage, not a safe persisted default; the submitted declaration owns it.
                if key not in file_match_keys:
                    parameter["default_value"] = witness["parameter_value"]
                parameters.append(parameter)
    for key in input_templated_keys:
        used_param_keys.add(key)
    for key in file_match_keys:
        used_param_keys.add(key)
        if key not in minted_input_witness_keys:
            parameters.append({"key": key})
    prelude_keys = [*input_templated_keys, *(key for key in file_match_keys if key not in input_templated_keys)]
    if prelude_keys:
        lines.extend(
            witness_prelude_lines(
                prelude_keys,
                include_month_helper=input_templated_needs_month,
                include_iso_date_helper=input_templated_needs_iso_date,
                include_period_helpers=input_templated_needs_period_helpers,
                collision_groups=input_templated_collision_groups,
            )
        )
        LOG.info(
            "copilot_spine_input_templated_prelude",
            witness_keys=prelude_keys,
            month_helper=input_templated_needs_month,
            iso_date_helper=input_templated_needs_iso_date,
            period_helpers=input_templated_needs_period_helpers,
        )
    if file_match_locator and file_match_transform is not None:
        lines.extend(_same_month_helper_lines())
        lines.append(f"{_INDENT}{_DOWNLOAD_TARGET_VAR} = {file_match_locator}")
        LOG.info(
            "copilot_spine_same_month_file_match_transform_applied",
            provenance_source=SAME_MONTH_FILE_MATCH_PROVENANCE_SOURCE,
            parameter_keys=list(file_match_transform.expected_declared_keys),
        )
        diagnostics.locator_provenance.append(
            {
                "trajectory_index": reached_download_target.trajectory_anchor
                if reached_download_target is not None and reached_download_target.trajectory_anchor is not None
                else -1,
                "selector": reached_download_target.selector if reached_download_target is not None else "",
                "emitted_literal": file_match_locator,
                "source": SAME_MONTH_FILE_MATCH_PROVENANCE_SOURCE,
                "date_keys": list(file_match_transform.date_keys),
                "expected_declared_keys": list(file_match_transform.expected_declared_keys),
                "provenance_fingerprint": file_match_transform.provenance_fingerprint,
                "date_format_id": file_match_transform.date_format_id,
                "holes": [
                    {
                        "declared_keys": list(hole.declared_keys),
                        "matched_literal": hole.matched_literal,
                        "position": hole.position,
                        "format_id": hole.format_id,
                        "source_values": list(hole.source_values),
                    }
                    for hole in file_match_transform.holes
                ],
            }
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

    def already_recorded(trajectory_index: int) -> bool:
        return any(
            record.get("trajectory_index") == trajectory_index
            for record in (*diagnostics.emitted_interactions, *diagnostics.dropped_interactions)
        )

    entry_url = ""
    entry_index = -1
    entry_replay_condition_active = False
    entry_replay_start_index = 0
    entry_post_auth_resume_index = 0
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
            _DOWNLOAD_TARGET_VAR
            if file_match_locator
            else f"page.locator({_py_str(reached_download_target.selector)})"
            if compile_download_target and reached_download_target is not None
            else ""
        )
        # Which element proves the block is where the flow starts is a separate question from which
        # step it resumes at: a scout that opened a password-reset link before signing in anchored the
        # whole login on the link it then clicked away from, while still needing to replay that click.
        # The anchor prefers a durable target - something the flow fills or selects - and the replay
        # start is left exactly where it was.
        durable_anchor_target, _durable_anchor_index = _entry_target_locator(
            entry_trajectory, strict_selectors=strict_selectors, prefer_durable=True
        )
        if fallback_entry_target and durable_anchor_target:
            # Re-anchoring to the first-touched element after navigating would put the block back on
            # the link it clicked away from, so the durable target is the anchor on both paths.
            fallback_entry_target = durable_anchor_target
        entry_target = download_entry_target if download_entry_target else fallback_entry_target
        entry_replay_condition_active = bool(download_entry_target and fallback_entry_target)
        # A skipped prefix is only forgivable when nothing in it would have been emitted; forgiving a
        # span that holds the read the block returns would drop that answer with no note anywhere.
        prefix_binds_output = any(_binds_block_output(step) for step in trajectory[entry_index:fallback_entry_index])
        entry_replay_start_index = (
            fallback_entry_index
            if fallback_entry_index > entry_index and not optional_dismissal_prefix and not prefix_binds_output
            else 0
        )
        if entry_index > 0:
            notes.append("entry URL taken from a later interaction; earlier steps had no source_url")
        if entry_replay_condition_active and fallback_entry_index > entry_index:
            notes.append("download fallback entry target taken from a later durable interaction")
        if entry_post_auth_resume_index:
            notes.append("entry fallback can resume after authentication when login controls stay hidden")
        elif entry_replay_start_index:
            notes.append("entry replay starts at a later durable interaction")
        entry_recovery_clicks: list[tuple[int, str]] = []
        if entry_replay_start_index:
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
                lines.append(
                    f'{_INDENT * recovery_indent}await {_ENTRY_TARGET_VAR}.wait_for(state="visible", timeout={_REQUIRED_STATE_TIMEOUT_MS})'
                )
            else:
                lines.append(
                    f'{_INDENT * post_goto_indent}await {_ENTRY_TARGET_VAR}.wait_for(state="visible", timeout={_REQUIRED_STATE_TIMEOUT_MS})'
                )
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
        return _INDENT

    def emit_observed_control_readiness(interaction: Mapping[str, Any], locator: str, action_indent: str) -> None:
        if interaction.get("observed_hidden") is True:
            lines.append(
                f'{action_indent}await {locator}.wait_for(state="visible", timeout={_REQUIRED_STATE_TIMEOUT_MS})'
            )
        if interaction.get("observed_disabled") is True:
            poll_rounds = _REQUIRED_STATE_TIMEOUT_MS // 1000
            lines.append(f"{action_indent}for _ in range({poll_rounds}):")
            lines.append(
                f"{action_indent}{_INDENT}if await {locator}.is_enabled() "
                f'and (await {locator}.get_attribute("data-disabled") or "").strip().lower() != "true":'
            )
            lines.append(f"{action_indent}{_INDENT * 2}break")
            lines.append(f"{action_indent}{_INDENT}await page.wait_for_timeout(1000)")
            lines.append(f"{action_indent}else:")
            lines.append(
                f"{action_indent}{_INDENT}raise Exception("
                f"{_py_str(f'Scout-observed control did not become enabled: {_step_target(interaction)}')})"
            )

    snapshot_recovery_emitted = False
    captcha_boundary_indices = _captcha_boundary_indices(trajectory)

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

    truncated_at_index = len(trajectory)
    for trajectory_index, interaction in enumerate(trajectory):
        if emitted >= _MAX_STEPS:
            diagnostics.truncated = True
            notes.append(f"trajectory truncated at {_MAX_STEPS} steps")
            truncated_at_index = trajectory_index
            break
        if entry_replay_start_index and trajectory_index < entry_replay_start_index:
            if not already_recorded(trajectory_index):
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
                notes.append("dropped a press_key interaction with no recorded key")
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
                emit_observed_control_readiness(interaction, locator, action_indent)
                lines.append(f"{action_indent}await {locator}.press({_py_str(key)})")
                record_emission(trajectory_index, tool_name, "press", locator, line_start=line_start)
            else:
                if strict_selectors:
                    if not already_recorded(trajectory_index):
                        notes.append("dropped an interaction with no selector")
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
            if trajectory_index in captcha_boundary_indices:
                lines.append(f"{action_indent}await solve_captcha(page)")
            append_step(f"Press {key}", "keypress", line_start)
            emitted += 1
            continue

        if tool_name == "read_value":
            expression = str(interaction.get("read_expression") or "").strip()
            output_path = str(interaction.get("read_output_path") or "").strip()
            if not expression or not output_path.startswith("output."):
                notes.append("dropped a read with no expression or no output binding")
                diagnostics.dropped_interactions.append(
                    {"trajectory_index": trajectory_index, "tool_name": tool_name, "reason_code": "missing_read"}
                )
                continue
            line_start = len(lines) + 1
            variable = f"_read_value_{len(read_bindings)}"
            # A read is only recorded once it returns something, so an empty replay contradicts the
            # proof whatever shape that proof had; an empty collection here is absence, not a correct
            # answer for a request that legitimately has none.
            absent = '(None, "", [], {})'
            lines.append(f"{action_indent}{variable} = await page.evaluate({expression!r})")
            poll_rounds = _REQUIRED_STATE_TIMEOUT_MS // 1000
            lines.append(f"{action_indent}for _ in range({poll_rounds}):")
            lines.append(f"{action_indent}{_INDENT}if {variable} not in {absent}:")
            lines.append(f"{action_indent}{_INDENT * 2}break")
            lines.append(f"{action_indent}{_INDENT}await page.wait_for_timeout(1000)")
            lines.append(f"{action_indent}{_INDENT}{variable} = await page.evaluate({expression!r})")
            # The scout only records a read that returned something, so an absent replay contradicts
            # the proof this read was built from. Returning the absent value instead reports success
            # while the requested field carries nothing.
            lines.append(f"{action_indent}if {variable} in {absent}:")
            lines.append(
                f"{action_indent}{_INDENT}raise Exception("
                f"{f'{output_path} was not present on the page: '!r} + {expression!r})"
            )
            read_bindings.append((output_path, variable))
            record_emission(trajectory_index, tool_name, "evaluate", "page", line_start=line_start, lane="page_read")
            append_step(f"Read {output_path.removeprefix('output.')}", "extract", line_start)
            emitted += 1
            continue

        if tool_name == "wait":
            try:
                duration_ms = int(interaction.get("duration_ms") or 0)
            except (TypeError, ValueError):
                duration_ms = 0
            if duration_ms <= 0:
                notes.append("dropped a wait interaction with no recorded duration")
                diagnostics.dropped_interactions.append(
                    {"trajectory_index": trajectory_index, "tool_name": tool_name, "reason_code": "missing_duration"}
                )
                continue
            line_start = len(lines) + 1
            lines.append(f"{action_indent}await page.wait_for_timeout({duration_ms})")
            record_emission(
                trajectory_index, tool_name, "wait_for_timeout", "page", line_start=line_start, lane="page_wait"
            )
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
                emit_observed_control_readiness(interaction, locator, action_indent)
                templating_plan = _input_templating_plan(interaction)
                if templating_plan is not None and templating_plan.surface == "row_text":
                    lines.append(f"{action_indent}if await {locator}.count() != 1:")
                    lines.append(
                        f"{action_indent}{_INDENT}raise Exception("
                        f"{_py_str('grounded statement row did not resolve uniquely')})"
                    )
                lines.append(f"{action_indent}await {locator}.click()")
                lines.append(f"{action_indent}await page.wait_for_load_state({_py_str(_DOMCONTENTLOADED)})")
                if trajectory_index in captcha_boundary_indices:
                    lines.append(f"{action_indent}await solve_captcha(page)")
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
                emit_observed_control_readiness(interaction, locator, action_indent)
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
            emit_observed_control_readiness(interaction, locator, action_indent)
            lines.append(f"{action_indent}{credential_fill_source(locator, credential_param_key, credential_field)}")
            record_emission(trajectory_index, tool_name, "fill", locator, line_start=line_start)
            # action_type values are ActionType members held as string literals, the same vocabulary
            # code_block_steps.py uses; there is no credential-fill member, and a fill is text entry.
            append_step(f"Fill {credential_field}", "input_text", line_start)
        elif tool_name == "select_option":
            emit_snapshot_recovery(trajectory_index, action_indent)
            value = str(interaction.get("value") or "").strip()
            if not value:
                notes.append("dropped a select_option interaction with no recorded value")
                diagnostics.dropped_interactions.append(
                    {"trajectory_index": trajectory_index, "tool_name": tool_name, "reason_code": "missing_value"}
                )
                continue
            emit_observed_control_readiness(interaction, locator, action_indent)
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
            record_emission(
                trajectory_index, tool_name, "hover", locator, line_start=line_start, lane="recording_hover"
            )
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

    # Single reconciliation point for the retained manifest: a branch that neither emits, drops, nor forgives
    # its index lands here as a drop. The post-truncation tail is unvisited, so the truncation finding owns it.
    laned_indices = _recorded_partition_indices(diagnostics)
    for trajectory_index in diagnostics.retained_trajectory_indices:
        if trajectory_index >= truncated_at_index or trajectory_index in laned_indices:
            continue
        unaccounted = trajectory[trajectory_index]
        notes.append(f"dropped an unaccounted {str(unaccounted.get('tool_name') or '')!r} interaction")
        diagnostics.dropped_interactions.append(
            {
                "trajectory_index": trajectory_index,
                "tool_name": str(unaccounted.get("tool_name") or ""),
                "selector": str(unaccounted.get("selector") or "").strip(),
                "reason_code": "unaccounted_branch",
            }
        )

    download_filename_for_return = ""
    if compile_download_target and reached_download_target is not None:
        # The download affordance is observed in nav_targets, not necessarily a trajectory click, so the
        # download is an appended terminal step compiled from the typed target — never an in-place click upgrade.
        # The worker-owned claim helper is the one terminal shape both engines execute: the sandboxed
        # runner cannot broker page.expect_download. The helper clicks once and confirms the fired
        # download; the bytes land wherever this run's download binding already sends them, and the
        # execution layer registers them from there.
        download_filename = _unique_key(_DOWNLOAD_FILENAME_VAR_BASE, used_download_vars)
        claim_selector = file_match_selector or _py_str(reached_download_target.selector)
        lines.append(f"{_INDENT}{download_filename} = await {DOWNLOAD_CLAIM_HELPER_NAME}(page, {claim_selector})")
        # Read bindings emit their own return below, and an extraction suffix appends one after this
        # block; either way a return here would make everything that follows unreachable, silently
        # dropping the reads the same turn was asked for. One return site, always the last.
        emit_download_return = emit_read_return and not read_bindings
        if not emit_download_return:
            download_filename_for_return = download_filename
        lines.extend(_download_summary_return_lines(download_filename, emit_download_return))

    if not lines:
        return None
    expected_binding_fingerprint_count = 1 if parameter_binding_snapshot is not None else 0
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
    if read_bindings and emit_read_return:
        return_root = _ExtractionReturnNode()
        # Distinct reads that share a path (only the anonymous path can) each keep their value under
        # a suffixed key: choosing one would silently discard evidence.
        seen_paths: dict[str, int] = {}
        for output_path, variable in read_bindings:
            occurrence = seen_paths.get(output_path, 0)
            seen_paths[output_path] = occurrence + 1
            keyed_path = output_path if occurrence == 0 else f"{output_path}_{occurrence + 1}"
            _set_return_expression(return_root, output_path_segments(keyed_path.removeprefix("output.")), variable)
        if download_filename_for_return:
            # The download terminal deferred its summary to this return so the reads survive.
            _set_return_expression(return_root, (("downloaded_file_name", False),), download_filename_for_return)
        lines.append(f"{_INDENT}return {_return_node_expression(return_root)}")
    if steps:
        steps[-1]["line_end"] = len(lines)

    diagnostics.emitted_interaction_count = emitted
    code = "\n".join(lines) + "\n"
    segments: list[SynthesizedCodeBlock] = []
    if not _segment_pass:
        # Each segment is synthesized from its own slice rather than sliced out of the code above, so it
        # carries its own prelude and guards and is valid, correctly scoped, and independently runnable.
        segment_bounds = credential_segment_bounds(trajectory) or []
        for segment_index, (start, end) in enumerate(segment_bounds):
            # Only the segment that ends at the affordance carries the download terminal; giving every
            # segment the target makes the login segment click a selector its page does not have.
            segment_download_target = reached_download_target if segment_index == len(segment_bounds) - 1 else None
            segment = synthesize_code_block(
                trajectory[start : end + 1],
                strict_selectors=strict_selectors,
                reached_download_target=segment_download_target,
                parameter_binding_snapshot=parameter_binding_snapshot,
                file_match_transform=file_match_transform if segment_download_target is not None else None,
                emit_read_return=emit_read_return,
                _segment_pass=True,
            )
            if segment is None or not segment.diagnostics.emitted_interaction_count:
                segments = []
                break
            segments.append(segment)
    return SynthesizedCodeBlock(
        code=code,
        parameters=parameters,
        notes=notes,
        diagnostics=diagnostics,
        steps=steps,
        segments=segments if len(segments) >= 2 else [],
    )


_LEADING_TAG_ID_SELECTOR_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*#")


def normalized_scout_selector(selector: str) -> str:
    # Capture and persist-seam comparison share one normal form: a leading `tag#id` qualifier reduces
    # to `#id` (ids are document-unique), so both sides name the same control.
    return _LEADING_TAG_ID_SELECTOR_RE.sub("#", selector)


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


def _download_summary_return_lines(download_filename: str, emit_read_return: bool) -> list[str]:
    """The filename summary the download terminal returns, or nothing when a suffix follows it.

    An extraction suffix is appended after this terminal, so returning here would make every
    extracted read unreachable. Registration never depends on this value — the execution layer
    derives `downloaded_files` from the run directory — so the suffix's own return carries the block.
    """
    if not emit_read_return:
        return []
    return [
        f"{_INDENT}return {{",
        f'{_INDENT * 2}"downloaded_file_name": {download_filename},',
        f"{_INDENT}}}",
    ]


# Model-owned slots the synthesizer cannot prove; the model fills these.


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
