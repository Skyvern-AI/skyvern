"""Deterministic synthesis of a copilot `code` block from a scout trajectory.

Pure module: ``synthesize_code_block`` is a function of its input trajectory
only — no LLM, no I/O, byte-identical output per trajectory. It turns the
scout's captured interaction sequence into a bounded, linear Playwright snippet
that runs on the raw ``page`` object the copilot code block executes against.
"""

from __future__ import annotations

import io
import json
import keyword
import re
import tokenize
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from skyvern.forge.sdk.copilot.composition_evidence import SCOUT_INTERACTION_EVIDENCE_TOOL
from skyvern.forge.sdk.copilot.reached_download_target import ReachedDownloadTarget
from skyvern.utils.strings import escape_code_fences

_MAX_STEPS = 60
_INDENT = "    "
_DOMCONTENTLOADED = "domcontentloaded"
_ENTRY_TARGET_VAR = "_scout_entry_target"
_ENTRY_REUSED_VAR = "_scout_entry_reused_current_page"
_ENTRY_RESUME_AFTER_AUTH_VAR = "_scout_entry_resume_after_auth"
_ENTRY_RESUME_TARGET_VAR = "_scout_entry_resume_target"
_ENTRY_OPENER_VAR = "_scout_entry_opener"
_OPTIONAL_DISMISSAL_VAR = "_scout_optional_dismissal"
_ENTRY_LOCATOR_VARS = (_ENTRY_TARGET_VAR, _ENTRY_RESUME_TARGET_VAR, _ENTRY_OPENER_VAR)
_INTERNAL_SCOUT_VARS = (
    _ENTRY_TARGET_VAR,
    _ENTRY_REUSED_VAR,
    _ENTRY_RESUME_AFTER_AUTH_VAR,
    _ENTRY_RESUME_TARGET_VAR,
    _ENTRY_OPENER_VAR,
    _OPTIONAL_DISMISSAL_VAR,
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
    locator_provenance: list[dict[str, Any]] = field(default_factory=list)
    # (trajectory enumerate index -> minted type_text parameter key); diagnostics-only, never serialized.
    # Recovers the key for a typed field whose value was withheld from default_value (typed_value == "").
    typed_param_bindings: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class SynthesizedCodeBlock:
    code: str
    parameters: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    diagnostics: SynthesisDiagnostics = field(default_factory=SynthesisDiagnostics)
    steps: list[dict[str, Any]] = field(default_factory=list)


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
        if ambiguous_role or _is_bare_ambiguous_selector(selector):
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
        if str(trajectory[index].get("tool_name") or "") == "click":
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


def synthesize_code_block(
    trajectory: Sequence[Mapping[str, Any]],
    *,
    strict_selectors: bool = False,
    reached_download_target: ReachedDownloadTarget | None = None,
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
    compile_download_target = (
        reached_download_target is not None
        and not reached_download_target.already_registered
        and bool(reached_download_target.selector)
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
        entry_recovery_clicks: list[str] = []
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
                    entry_recovery_clicks.append(recovery_locator)
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
                for recovery_locator in entry_recovery_clicks:
                    lines.append(f"{_INDENT * recovery_indent}{_ENTRY_OPENER_VAR} = {recovery_locator}")
                    lines.append(f"{_INDENT * recovery_indent}if await {_ENTRY_OPENER_VAR}.count() == 1:")
                    lines.append(f"{_INDENT * (recovery_indent + 1)}await {_ENTRY_OPENER_VAR}.click()")
                    lines.append(
                        f"{_INDENT * (recovery_indent + 1)}await page.wait_for_load_state({_py_str(_DOMCONTENTLOADED)})"
                    )
                lines.append(f'{_INDENT * recovery_indent}await {_ENTRY_TARGET_VAR}.wait_for(state="visible")')
            else:
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
        append_step(f"Open {entry_url}", "goto_url", line_start)

    emitted = 0

    def action_indent_for(trajectory_index: int) -> str:
        if entry_replay_condition_active:
            if entry_post_auth_resume_index and trajectory_index < entry_post_auth_resume_index:
                return _INDENT * 3
            return _INDENT * 2
        if entry_post_auth_resume_index and trajectory_index < entry_post_auth_resume_index:
            return _INDENT * 2
        return _INDENT

    for trajectory_index, interaction in enumerate(trajectory):
        if emitted >= _MAX_STEPS:
            diagnostics.truncated = True
            notes.append(f"trajectory truncated at {_MAX_STEPS} steps")
            break
        if entry_replay_start_index and trajectory_index < entry_replay_start_index:
            continue
        action_indent = action_indent_for(trajectory_index)
        tool_name = str(interaction.get("tool_name") or "")

        if tool_name == "press_key":
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
            lines.append(f"{action_indent}await page.wait_for_load_state({_py_str(_DOMCONTENTLOADED)})")
            append_step(f"Press {key}", "keypress", line_start)
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
            if _is_optional_or_structural_dismissal_click(interaction):
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
            else:
                lines.append(f"{action_indent}await {locator}.click()")
                lines.append(f"{action_indent}await page.wait_for_load_state({_py_str(_DOMCONTENTLOADED)})")
            append_step(f"Click {_step_target(interaction)}", "click", line_start)
        elif tool_name == "type_text":
            typed_identity = _typed_value_identity(interaction)
            param_key = typed_param_keys.get(typed_identity) if typed_identity is not None else None
            if param_key is None:
                param_key = _param_key(interaction, used_param_keys)
                parameter = {"key": param_key}
                typed_value = str(interaction.get("typed_value") or "").strip()
                if typed_value:
                    parameter["default_value"] = typed_value
                typed_length = interaction.get("typed_length")
                if strict_selectors and typed_length is not None:
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
            lines.append(f"{action_indent}await {locator}.fill(str({param_key}))")
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
        elif tool_name == "select_option":
            value = str(interaction.get("value") or "").strip()
            if not value:
                notes.append("dropped a select_option interaction with no recorded value")
                diagnostics.dropped_interactions.append(
                    {"trajectory_index": trajectory_index, "tool_name": tool_name, "reason_code": "missing_value"}
                )
                continue
            lines.append(f"{action_indent}await {locator}.select_option({_py_str(value)})")
            lines.append(f"{action_indent}await page.wait_for_load_state({_py_str(_DOMCONTENTLOADED)})")
            append_step(f"Select {value} in {_step_target(interaction)}", "select_option", line_start)
        else:
            notes.append(f"skipped unsupported interaction tool_name={tool_name!r}")
            diagnostics.dropped_interactions.append(
                {"trajectory_index": trajectory_index, "tool_name": tool_name, "reason_code": "unsupported_tool"}
            )
            continue
        emitted += 1

    if entry_replay_condition_active and emitted == 0 and not entry_post_auth_resume_index:
        lines.append(f"{_INDENT * 2}pass")

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
    parts = [
        "SYNTHESIZED CODE BLOCK (offered once). The page interactions you scouted were compiled into a "
        "deterministic Playwright snippet. Persist it VERBATIM as a `code` block labeled "
        f"`{_SYNTHESIZED_BLOCK_LABEL}` via update_workflow / update_and_run_blocks; hand-author the "
        "data-capture step it does not cover so the block `return`s a keyed structure (a dict, or an array "
        "of objects for repeated records) — never a flat `page.inner_text(...)` / `text_content(...)` blob.",
        "```python",
        synthesized.code.rstrip("\n"),
        "```",
    ]
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
