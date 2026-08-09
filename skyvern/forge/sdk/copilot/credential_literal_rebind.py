from __future__ import annotations

import ast
import re
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping, Sequence

import structlog
import yaml

from skyvern.forge.sdk.copilot.code_block_synthesis import (
    CREDENTIAL_FILL_TOOL_NAME,
    block_has_unguarded_credential_fill,
    credential_fill_source,
    wrapped_code_ast,
)
from skyvern.forge.sdk.copilot.credential_fill_fields import CREDENTIAL_FILL_FIELDS
from skyvern.forge.sdk.copilot.workflow_credential_utils import credential_param_ids, workflow_blocks

LOG = structlog.get_logger()

_Verdict = Literal["matched", "other", "unresolvable"]

_FILL_METHODS = frozenset({"fill", "type", "press_sequentially"})
# `wrapped_code_ast` re-indents every line by four spaces, so AST columns are shifted by that much.
_WRAP_INDENT = 4

_IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")
_STRING_LITERAL_RE = re.compile(r"(?P<q>['\"])(?:\\.|(?!(?P=q)).)*(?P=q)")
_STRING_ASSIGN_TMPL = r"^[ \t]*{name}[ \t]*=[ \t]*" + _STRING_LITERAL_RE.pattern + r"[ \t]*\n?"
_GOTO_RE = re.compile(r"\.goto\(\s*['\"]([^'\"]+)['\"]")
_LOCATOR_ALIAS_RE = re.compile(
    r"^[ \t]*(?P<name>[A-Za-z_]\w*)[ \t]*=[ \t]*page\.locator\(\s*(?P<q>['\"])(?P<sel>(?:\\.|(?!(?P=q)).)*)(?P=q)\s*\)"
    r"(?:\s*\.\s*(?:first|last))?[ \t]*$",
    re.MULTILINE,
)


def _locator_alias_bindings(code: str) -> tuple[dict[str, str], set[str]]:
    """(alias -> selector, conflicted aliases). A name bound to a single literal ``page.locator("<sel>")``
    maps to that selector; a name rebound to a different selector is ambiguous and dropped into the
    conflicted set, where a fill through it must fall to the fail-closed backstop rather than be rebound."""
    aliases: dict[str, str] = {}
    conflicted: set[str] = set()
    for match in _LOCATOR_ALIAS_RE.finditer(code):
        name, selector = match.group("name"), match.group("sel")
        if name in aliases and aliases[name] != selector:
            conflicted.add(name)
        aliases.setdefault(name, selector)
    for name in conflicted:
        aliases.pop(name, None)
    return aliases, conflicted


def _locator_alias_selectors(code: str) -> dict[str, str]:
    return _locator_alias_bindings(code)[0]


def _normalize_page_url(url: str) -> str:
    return url.split("#", 1)[0].strip().rstrip("/")


@dataclass(frozen=True)
class CredentialRebindSkip:
    stage: str
    selector: str


@dataclass(frozen=True)
class CredentialRebindResult:
    workflow_yaml: str
    changed: bool
    rebound: tuple[str, ...]
    authored: tuple[tuple[str, str], ...] = ()
    skips: tuple[CredentialRebindSkip, ...] = ()
    # A fill at a scouted credential selector still holds a non-parameter value the rewriters could not
    # neutralize (e.g. a secret inside a nested call). The output-policy secret scan does not catch every
    # such shape, so the caller must fail closed on this rather than admit the submission.
    residual_selectors: tuple[str, ...] = ()


def scouted_credential_targets(scout_trajectory: Sequence[Mapping[str, Any]] | None) -> dict[str, tuple[str, str]]:
    """Map each scouted selector to the (credential_id, field) `fill_credential_field` typed into it, dropping any selector scouted with conflicting credential/field targets so its literals fall to the fail-closed refusal backstop rather than being rebound to an ambiguous parameter."""
    targets: dict[str, tuple[str, str]] = {}
    conflicted: set[str] = set()
    for interaction in scout_trajectory or []:
        if not isinstance(interaction, Mapping):
            continue
        if str(interaction.get("tool_name") or "").strip() != CREDENTIAL_FILL_TOOL_NAME:
            continue
        selector = str(interaction.get("selector") or "").strip()
        credential_id = str(interaction.get("credential_id") or "").strip()
        field = str(interaction.get("credential_field") or "").strip()
        if not selector or not credential_id or field not in CREDENTIAL_FILL_FIELDS:
            continue
        mapping = (credential_id, field)
        existing = targets.get(selector)
        if existing is not None and existing != mapping:
            conflicted.add(selector)
            continue
        targets.setdefault(selector, mapping)
    for selector in conflicted:
        targets.pop(selector, None)
    return targets


def scouted_selector_source_urls(scout_trajectory: Sequence[Mapping[str, Any]] | None) -> dict[str, frozenset[str]]:
    """Map each `fill_credential_field` selector to the normalized page URL(s) it was scouted on, so a
    block that navigates elsewhere cannot rebind a same-named selector to a credential scouted on a
    different page."""
    by_selector: dict[str, set[str]] = {}
    for interaction in scout_trajectory or []:
        if not isinstance(interaction, Mapping):
            continue
        if str(interaction.get("tool_name") or "").strip() != CREDENTIAL_FILL_TOOL_NAME:
            continue
        selector = str(interaction.get("selector") or "").strip()
        source_url = _normalize_page_url(str(interaction.get("source_url") or ""))
        if not selector or not source_url:
            continue
        by_selector.setdefault(selector, set()).add(source_url)
    return {selector: frozenset(urls) for selector, urls in by_selector.items()}


def scouted_element_fingerprints(scout_trajectory: Sequence[Mapping[str, Any]] | None) -> dict[str, dict[str, str]]:
    """Extract element fingerprint from fill_credential_field interactions by scouted selector, dropping any
    selector scouted with differing fingerprints across pages. `scouted_selector_source_urls` keeps every
    page a selector was scouted on, so keeping one page's fingerprint would compare a fill on another page
    against the wrong element and resolve 'other'; an ambiguous selector falls to the fail-closed backstop."""
    fingerprints: dict[str, dict[str, str]] = {}
    conflicted: set[str] = set()
    for interaction in scout_trajectory or []:
        if not isinstance(interaction, Mapping):
            continue
        if str(interaction.get("tool_name") or "").strip() != CREDENTIAL_FILL_TOOL_NAME:
            continue
        selector = str(interaction.get("selector") or "").strip()
        if not selector:
            continue
        fp = {}
        for key in {
            "element_fingerprint_id",
            "element_fingerprint_name",
            "element_fingerprint_type",
            "element_fingerprint_placeholder",
            "element_fingerprint_label",
            "element_fingerprint_test_id",
            "element_fingerprint_tag",
            "element_fingerprint_probed",
            "role",
            "accessible_name",
        }:
            value = str(interaction.get(key) or "").strip()
            if value:
                attr_name = key.replace("element_fingerprint_", "")
                fp[attr_name] = value
        if fp:
            existing = fingerprints.get(selector)
            if existing is not None and existing != fp:
                conflicted.add(selector)
                continue
            fingerprints.setdefault(selector, fp)
    for selector in conflicted:
        fingerprints.pop(selector, None)
    return fingerprints


def _existing_param_key(parameters: Sequence[object], credential_id: str) -> str | None:
    for key, credential_ids in credential_param_ids(parameters).items():
        if credential_id in credential_ids:
            return key
    return None


def _mint_param_key(credential_id: str, used: set[str]) -> str:
    suffix = re.sub(r"\W", "", credential_id)[-8:] or "login"
    candidate = f"credential_{suffix}"
    index = 2
    while candidate in used:
        candidate = f"credential_{suffix}_{index}"
        index += 1
    used.add(candidate)
    return candidate


def _identifier_bound_to_string_literal(code: str, name: str) -> bool:
    pattern = re.compile(_STRING_ASSIGN_TMPL.format(name=re.escape(name)), re.MULTILINE)
    return pattern.search(code) is not None


def _drop_dead_string_assignment(code: str, name: str) -> str:
    pattern = re.compile(_STRING_ASSIGN_TMPL.format(name=re.escape(name)), re.MULTILINE)
    match = pattern.search(code)
    if not match:
        return code
    remaining = code[: match.start()] + code[match.end() :]
    # `credential_x.username` must not count as a use of a local named `username`.
    if re.search(r"(?<![\w.])" + re.escape(name) + r"\b", remaining):
        return code
    return remaining


def _block_page_incompatible(block_goto_urls: set[str], target_source_urls: frozenset[str]) -> bool:
    # A block counts as a different page only when it navigates yet lands on none of the scout pages;
    # scoping is block-level, so a multi-goto block that mixes pages leans on the refusal backstop.
    return bool(block_goto_urls) and bool(target_source_urls) and not (block_goto_urls & target_source_urls)


def _fill_value_node(node: ast.Call, positional_index: int) -> ast.AST | None:
    """The value a fill types: the positional arg at ``positional_index``, else a ``value=`` keyword.
    Playwright accepts either, and a raw secret passed by keyword must be neutralized just the same."""
    if len(node.args) > positional_index:
        return node.args[positional_index]
    for keyword in node.keywords:
        if keyword.arg == "value":
            return keyword.value
    return None


def _fill_call_selector_and_value(
    node: ast.Call, alias_selectors: Mapping[str, str] | None = None
) -> tuple[str | None, ast.AST] | None:
    """For a fill/type/press_sequentially call, return (string-literal selector, value node). The selector
    is None when the target locator is not a recognizable ``page.fill("<sel>", ...)`` or
    ``page.locator("<sel>")...`` string-literal shape (e.g. ``get_by_placeholder``), or an alias variable
    that ``alias_selectors`` cannot resolve back to a literal ``page.locator("<sel>")``. Returns None only
    when there is no value to inspect at all."""
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in _FILL_METHODS:
        return None
    receiver = func.value
    if isinstance(receiver, ast.Name) and receiver.id == "page":
        first = node.args[0] if node.args else None
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            value = _fill_value_node(node, 1)
            return (first.value, value) if value is not None else None
        value = _fill_value_node(node, 1)
        return (None, value) if value is not None else None
    base = receiver
    while isinstance(base, ast.Attribute) and base.attr in {"first", "last"}:
        base = base.value
    value = _fill_value_node(node, 0)
    if value is None:
        return None
    if (
        isinstance(base, ast.Call)
        and isinstance(base.func, ast.Attribute)
        and base.func.attr == "locator"
        and isinstance(base.func.value, ast.Name)
        and base.func.value.id == "page"
        and base.args
        and isinstance(base.args[0], ast.Constant)
        and isinstance(base.args[0].value, str)
    ):
        return base.args[0].value, value
    if isinstance(base, ast.Name) and alias_selectors:
        selector = alias_selectors.get(base.id)
        if selector:
            return selector, value
    return None, value


def _value_is_param_field(value: ast.AST, param_key: str, field: str) -> bool:
    if field == "totp":
        target = value.value if isinstance(value, ast.Await) else value
        return (
            isinstance(target, ast.Call)
            and isinstance(target.func, ast.Attribute)
            and target.func.attr == "otp"
            and isinstance(target.func.value, ast.Name)
            and target.func.value.id == param_key
        )
    return (
        isinstance(value, ast.Attribute)
        and value.attr == field
        and isinstance(value.value, ast.Name)
        and value.value.id == param_key
    )


def _embeds_string_literal(node: ast.AST) -> bool:
    """True when the expression inlines a non-empty string literal anywhere in its value — directly, in an
    f-string, or built up via concatenation, ``%``/``.format`` templating, or ``"".join([...])``. These are
    the shapes that can carry a raw secret. A subscript is a runtime lookup, so its key is not counted; pure
    runtime references (attribute chains, bare names) inline no literal and are left alone."""
    target = node.value if isinstance(node, ast.Await) else node
    if isinstance(target, ast.Constant):
        return isinstance(target.value, str) and target.value != ""
    if isinstance(target, ast.Subscript):
        return _embeds_string_literal(target.value)
    return any(_embeds_string_literal(child) for child in ast.iter_child_nodes(target))


def _value_is_inline_literal_fill(value: ast.AST, code: str) -> bool:
    """Whether a fill value carries an inline secret that must be neutralized, versus a runtime reference
    (foreign credential parameter, subscript, identifier bound to a call) that the shipped backstop leaves
    untouched. Only inline literals — and identifiers bound to one in the same block — are rewritten."""
    if _embeds_string_literal(value):
        return True
    target = value.value if isinstance(value, ast.Await) else value
    return isinstance(target, ast.Name) and _identifier_bound_to_string_literal(code, target.id)


def _standalone_fill_statements(tree: ast.AST) -> dict[int, ast.stmt]:
    """Map each fill call that IS a whole statement (bare or awaited) to that statement, so only a call
    whose result nothing consumes may be rewritten wholesale."""
    standalone: dict[int, ast.stmt] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr):
            continue
        value = node.value
        if isinstance(value, ast.Await):
            value = value.value
        if isinstance(value, ast.Call):
            standalone[id(value)] = node
    return standalone


def _scan_block_fills(code: str) -> tuple[list[tuple[str, ast.AST, ast.Call, ast.stmt | None]], list[ast.AST]]:
    """(recognized fills, values of fills whose receiver could not be resolved to a selector). The
    unrecognized VALUES are returned — not just a flag — because protection keys on the value shape: an
    inline literal at an unidentifiable receiver must still fail closed, whatever the locator spelling."""
    tree = wrapped_code_ast(code)
    if tree is None:
        return [], []
    fills: list[tuple[str, ast.AST, ast.Call, ast.stmt | None]] = []
    unrecognized_values: list[ast.AST] = []
    alias_selectors = _locator_alias_selectors(code)
    standalone = _standalone_fill_statements(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        found = _fill_call_selector_and_value(node, alias_selectors)
        if found is None:
            continue
        selector, value = found
        if selector is None:
            unrecognized_values.append(value)
        else:
            fills.append((selector, value, node, standalone.get(id(node))))
    return fills, unrecognized_values


def _node_source(lines: Sequence[str], node: ast.expr) -> str | None:
    """Source text of ``node``, undoing the wrapper's line and indent offsets. Multi-line spans are not
    reconstructed — the caller falls back to the fail-closed skip."""
    start = node.lineno - 2
    end = (node.end_lineno or node.lineno) - 2
    start_col = node.col_offset - _WRAP_INDENT
    end_col = (node.end_col_offset or 0) - _WRAP_INDENT
    if start != end or not (0 <= start < len(lines)) or start_col < 0 or end_col < start_col:
        return None
    line = lines[start]
    if end_col > len(line):
        return None
    return line[start_col:end_col]


def _replace_fill_statement(code: str, stmt: ast.stmt, locator_expr: str, param_key: str, field: str) -> str | None:
    """Rewrite a whole fill statement into the sanctioned emission, so the raw value is replaced rather
    than joined by a second fill the secret scan would still reject."""
    lines = code.splitlines()
    start = stmt.lineno - 2
    end = (stmt.end_lineno or stmt.lineno) - 2
    start_col = stmt.col_offset - _WRAP_INDENT
    if not (0 <= start <= end < len(lines)) or start_col < 0:
        return None
    if lines[start][:start_col].strip():
        return None
    indent = lines[start][:start_col]
    lines[start : end + 1] = [indent + credential_fill_source(locator_expr, param_key, field)]
    return "\n".join(lines) + ("\n" if code.endswith("\n") else "")


def _fill_locator_expr(lines: Sequence[str], call: ast.Call, selector: str) -> str | None:
    """The locator expression to re-emit the fill against: the draft's own receiver when it has one, else
    the scouted selector for the two-argument ``page.fill("<sel>", value)`` shape."""
    func = call.func
    if not isinstance(func, ast.Attribute):
        return None
    if (
        isinstance(func.value, ast.Name)
        and func.value.id == "page"
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
    ):
        return f"page.locator({_py_selector(selector)})"
    return _node_source(lines, func.value)


def _has_positive_page_evidence(
    block_goto_urls: set[str], target_source_urls: frozenset[str], sibling_source_urls: frozenset[str]
) -> bool:
    if target_source_urls and block_goto_urls and (block_goto_urls & target_source_urls):
        return True
    return bool(target_source_urls) and target_source_urls == sibling_source_urls


def _py_selector(selector: str) -> str:
    return '"' + selector.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _author_missing_credential_fill(
    code: str,
    selector: str,
    param_key: str,
    field: str,
    sibling_fields: Mapping[str, str],
    credential_parameter_keys: AbstractSet[str],
) -> str | None:
    """Insert a sanctioned credential fill for ``selector`` immediately after a sibling credential fill of
    the same parameter, matching the sibling's indentation. Returns the new code only when the authored
    fill is admitted by the persist seam's presence-guard rule, else None."""
    tree = wrapped_code_ast(code)
    if tree is None:
        return None
    lines = code.splitlines()
    insert_after: int | None = None
    indent = ""
    alias_selectors = _locator_alias_selectors(code)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        found = _fill_call_selector_and_value(node, alias_selectors)
        if found is None:
            continue
        sibling_selector, value = found
        if sibling_selector is None or sibling_selector not in sibling_fields:
            continue
        if not _value_is_param_field(value, param_key, sibling_fields[sibling_selector]):
            continue
        start_idx = node.lineno - 2
        end_idx = (node.end_lineno or node.lineno) - 2
        if 0 <= start_idx < len(lines):
            stripped = lines[start_idx].lstrip()
            indent = lines[start_idx][: len(lines[start_idx]) - len(stripped)]
            insert_after = end_idx
            break
    if insert_after is None:
        return None
    authored_line = indent + credential_fill_source(f"page.locator({_py_selector(selector)})", param_key, field)
    new_lines = lines[: insert_after + 1] + [authored_line] + lines[insert_after + 1 :]
    new_code = "\n".join(new_lines) + ("\n" if code.endswith("\n") else "")
    if block_has_unguarded_credential_fill(new_code, credential_parameter_keys):
        return None
    return new_code


def _rebind_block_code(
    code: str,
    targets: Mapping[str, tuple[str, str]],
    param_key_for: Callable[[str], str],
    source_urls_by_selector: Mapping[str, frozenset[str]],
    fingerprints_by_selector: Mapping[str, dict[str, str]] | None = None,
) -> tuple[str, list[str], set[str]]:
    rebound: list[str] = []
    used_params: set[str] = set()
    replaced_identifiers: set[str] = set()
    new_code = code
    block_goto_urls = {_normalize_page_url(url) for url in _GOTO_RE.findall(code)}
    alias_selectors = _locator_alias_selectors(code)
    if fingerprints_by_selector is None:
        fingerprints_by_selector = {}

    for selector, (credential_id, field) in targets.items():
        if _block_page_incompatible(block_goto_urls, source_urls_by_selector.get(selector, frozenset())):
            continue
        param_key = param_key_for(credential_id)
        access = f"{param_key}.{field}"
        selector_pattern = re.escape(selector)
        patterns = [
            re.compile(
                r"(\.(?:fill|type|press_sequentially)\(\s*['\"]" + selector_pattern + r"['\"]\s*,\s*)([^),]+?)(\s*\))"
            ),
            re.compile(
                r"(\.locator\(\s*['\"]"
                + selector_pattern
                + r"['\"]\s*\)\s*\.(?:fill|type|press_sequentially)\(\s*)([^),]+?)(\s*\))"
            ),
        ]
        patterns.extend(
            re.compile(
                r"(\b"
                + re.escape(alias_name)
                + r"(?:\s*\.\s*(?:first|last))?\s*\.(?:fill|type|press_sequentially)\(\s*)([^),]+?)(\s*\))"
            )
            for alias_name, alias_selector in alias_selectors.items()
            if alias_selector == selector
        )

        def _substitute(match: re.Match[str]) -> str:
            argument = match.group(2).strip()
            if argument == access:
                used_params.add(param_key)
                return match.group(0)
            bound_identifier = _IDENTIFIER_RE.fullmatch(argument) is not None and _identifier_bound_to_string_literal(
                code, argument
            )
            if not _STRING_LITERAL_RE.fullmatch(argument) and not bound_identifier:
                return match.group(0)
            if bound_identifier:
                replaced_identifiers.add(argument)
            rebound.append(f"{param_key}.{field}")
            used_params.add(param_key)
            return match.group(1) + access + match.group(3)

        for pattern in patterns:
            new_code = pattern.sub(_substitute, new_code)

    for receiver, _value, _node, _stmt in _inline_literal_fill_receivers(new_code):
        if receiver.kind == "css" and receiver.css_selector in targets:
            continue
        verdict, matched = _best_fingerprint_verdict(
            receiver, targets, fingerprints_by_selector, source_urls_by_selector, block_goto_urls
        )
        LOG.info(
            "copilot credential fill fingerprint verdict",
            receiver=receiver.css_selector or receiver.method or "unresolvable",
            verdict=verdict,
            matched_attributes=_receiver_attributes(receiver) if verdict == "matched" else [],
        )

    for _ in range(len(_inline_literal_fill_receivers(new_code)) + 1):
        repaired = False
        for receiver, _value, _node, fill_stmt in _inline_literal_fill_receivers(new_code):
            if fill_stmt is None or (receiver.kind == "css" and receiver.css_selector in targets):
                continue
            _verdict, matched = _best_fingerprint_verdict(
                receiver, targets, fingerprints_by_selector, source_urls_by_selector, block_goto_urls
            )
            if matched is None:
                continue
            scouted_selector, credential_id, field = matched
            param_key = param_key_for(credential_id)
            new_src = _replace_fill_statement(
                new_code, fill_stmt, f"page.locator({_py_selector(scouted_selector)})", param_key, field
            )
            if new_src is None:
                continue
            new_code = new_src
            rebound.append(f"{param_key}.{field}")
            used_params.add(param_key)
            repaired = True
            break
        if not repaired:
            break

    for identifier in replaced_identifiers:
        new_code = _drop_dead_string_assignment(new_code, identifier)

    return new_code, rebound, used_params


def _residual_raw_credential_fills(
    blocks: Sequence[Any],
    targets: Mapping[str, tuple[str, str]],
    source_urls_by_selector: Mapping[str, frozenset[str]],
    assigned: Mapping[str, str],
    fingerprints_by_selector: Mapping[str, dict[str, str]] | None = None,
) -> list[str]:
    """A leftover inline literal at the scouted selector fails closed; any other inline-literal fill is
    resolved against the target fingerprint, where 'matched'/'unresolvable' fail closed and only a provably
    'other' element is admitted. Rejection is unconditional — a satisfied sanctioned fill no longer masks an
    extra unresolved literal."""
    if fingerprints_by_selector is None:
        fingerprints_by_selector = {}
    residual: list[str] = []
    for selector, (credential_id, field) in targets.items():
        param_key = assigned.get(credential_id)
        if param_key is None:
            continue
        target_source_urls = source_urls_by_selector.get(selector, frozenset())
        fingerprint = fingerprints_by_selector.get(selector, {})
        for block in blocks:
            if not isinstance(block, dict):
                continue
            code = block.get("code")
            if not isinstance(code, str) or not code.strip():
                continue
            if _block_page_incompatible({_normalize_page_url(u) for u in _GOTO_RE.findall(code)}, target_source_urls):
                continue
            fills, _ = _scan_block_fills(code)
            recognized_literal = any(
                fsel == selector
                and _value_is_inline_literal_fill(fval, code)
                and not _value_is_param_field(fval, param_key, field)
                for fsel, fval, _n, _s in fills
            )
            flagged = recognized_literal or any(
                not (receiver.kind == "css" and receiver.css_selector in targets)
                and _fingerprint_verdict(receiver, fingerprint) in ("matched", "unresolvable")
                for receiver, _value, _node, _stmt in _inline_literal_fill_receivers(code)
            )
            if flagged:
                if selector not in residual:
                    residual.append(selector)
                break
    return residual


_SEMANTIC_METHOD_ATTR = {
    "get_by_placeholder": "placeholder",
    "get_by_label": "label",
    "get_by_test_id": "test_id",
}
_CSS_ATTR_KEYS = {"type": "type", "name": "name", "placeholder": "placeholder", "data-testid": "test_id"}
_CSS_ATTR_RE = re.compile(r"\[\s*([\w-]+)\s*=\s*(['\"]?)(.*?)\2\s*\]")
_CSS_UNSUPPORTED_CHARS = set(" >+~.:*/|,")
# HTML matches tag names and the `type` attribute case-insensitively, so comparing them exactly would
# resolve 'other' for a selector that still reaches the credential element.
_CSS_CASE_INSENSITIVE_ATTRS = frozenset({"tag", "type"})
# `get_by_test_id` matches exactly; the other semantic locators default to Playwright's relaxed matching.
_SEMANTIC_EXACT_METHODS = frozenset({"get_by_test_id"})


@dataclass(frozen=True)
class _FillReceiver:
    kind: Literal["css", "semantic", "unresolvable"]
    css_selector: str | None = None
    method: str | None = None
    argument: str | None = None
    role: str | None = None
    accessible_name: str | None = None


def _constant_str(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _semantic_receiver(node: ast.Call) -> _FillReceiver:
    """Classify a non-CSS fill receiver as a semantic locator (get_by_placeholder/label/test_id/role) or
    unresolvable. Any locator whose identifying argument is not a single string literal (regex, extra
    kwargs, unknown get_by_*) fails closed to 'unresolvable'."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return _FillReceiver(kind="unresolvable")
    base = func.value
    while isinstance(base, ast.Attribute) and base.attr in {"first", "last"}:
        base = base.value
    if not (isinstance(base, ast.Call) and isinstance(base.func, ast.Attribute)):
        return _FillReceiver(kind="unresolvable")
    method = base.func.attr
    if method in _SEMANTIC_METHOD_ATTR:
        if len(base.args) != 1 or base.keywords:
            return _FillReceiver(kind="unresolvable")
        argument = _constant_str(base.args[0])
        if argument is None:
            return _FillReceiver(kind="unresolvable")
        return _FillReceiver(kind="semantic", method=method, argument=argument)
    if method == "get_by_role":
        role = _constant_str(base.args[0]) if base.args else None
        if role is None:
            return _FillReceiver(kind="unresolvable")
        name = next((_constant_str(kw.value) for kw in base.keywords if kw.arg == "name"), None)
        return _FillReceiver(kind="semantic", method=method, role=role, accessible_name=name)
    return _FillReceiver(kind="unresolvable")


def _inline_literal_fill_receivers(code: str) -> list[tuple[_FillReceiver, ast.AST, ast.Call, ast.stmt | None]]:
    """(receiver, value, call, standalone statement) for every fill whose value inlines a literal secret,
    across both CSS and semantic-locator receivers."""
    tree = wrapped_code_ast(code)
    if tree is None:
        return []
    alias_selectors = _locator_alias_selectors(code)
    standalone = _standalone_fill_statements(tree)
    out: list[tuple[_FillReceiver, ast.AST, ast.Call, ast.stmt | None]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        found = _fill_call_selector_and_value(node, alias_selectors)
        if found is None:
            continue
        selector, value = found
        if not _value_is_inline_literal_fill(value, code):
            continue
        receiver = (
            _FillReceiver(kind="css", css_selector=selector) if selector is not None else _semantic_receiver(node)
        )
        out.append((receiver, value, node, standalone.get(id(node))))
    return out


def _parse_css_selector_attributes(selector: str) -> dict[str, str]:
    """Extract fingerprint-comparable attributes from the common CSS grammar (#id, tag, tag#id,
    [attr="v"] over type/name/placeholder/data-testid, and combinations). Classes, combinators, pseudo
    selectors, XPath, and unknown attributes are outside the grammar and return {} (unresolvable)."""
    selector = (selector or "").strip()
    if not selector:
        return {}
    remainder = _CSS_ATTR_RE.sub("", selector)
    if any(ch in _CSS_UNSUPPORTED_CHARS for ch in remainder):
        return {}
    result: dict[str, str] = {}
    for match in _CSS_ATTR_RE.finditer(selector):
        key = _CSS_ATTR_KEYS.get(match.group(1).strip())
        if key is None:
            return {}
        result[key] = match.group(3).strip()
    id_match = re.search(r"#([\w-]+)", remainder)
    if id_match:
        result["id"] = id_match.group(1)
    tag_match = re.match(r"([A-Za-z][\w-]*)", remainder)
    if tag_match:
        result["tag"] = tag_match.group(1)
    return result


def _receiver_attributes(receiver: _FillReceiver) -> list[str]:
    if receiver.kind == "css" and receiver.css_selector is not None:
        return sorted(_parse_css_selector_attributes(receiver.css_selector))
    if receiver.kind == "semantic":
        if receiver.method == "get_by_role":
            return ["role", "accessible_name"]
        attr = _SEMANTIC_METHOD_ATTR.get(receiver.method or "")
        return [attr] if attr else []
    return []


def _probed_attributes(fingerprint: Mapping[str, str]) -> frozenset[str]:
    """Attributes the capture probe actually read. Absent for a failed probe or a pre-fingerprint record,
    which keeps those undecidable rather than reading silence as proof the element lacks the attribute."""
    return frozenset(attr for attr in (fingerprint.get("probed") or "").split(",") if attr)


def _normalized_text(value: str) -> str:
    return " ".join(value.split()).casefold()


def _locator_text_matches(argument: str, fingerprint_value: str) -> bool:
    """Playwright's default (`exact=False`) text matching: case-insensitive, whitespace-collapsed substring.
    Comparing exactly instead would resolve 'other' for a locator that still reaches the credential element
    at runtime, admitting its literal."""
    return _normalized_text(argument) in _normalized_text(fingerprint_value)


def _css_fingerprint_verdict(selector: str, fingerprint: Mapping[str, str]) -> _Verdict:
    attrs = _parse_css_selector_attributes(selector)
    if not attrs:
        return "unresolvable"
    probed = _probed_attributes(fingerprint)
    verdict: _Verdict = "matched"
    for attr, val in attrs.items():
        fp_val = fingerprint.get(attr)
        if not fp_val:
            if attr in probed:
                return "other"
            # Unprobed silence outranks a mismatch already seen: identity is undecidable, and only
            # "unresolvable" fails closed while "other" admits the literal.
            return "unresolvable"
        if attr in _CSS_CASE_INSENSITIVE_ATTRS:
            if fp_val.strip().casefold() != val.strip().casefold():
                verdict = "other"
        elif fp_val.strip() != val.strip():
            verdict = "other"
    return verdict


def _semantic_fingerprint_verdict(receiver: _FillReceiver, fingerprint: Mapping[str, str]) -> _Verdict:
    if receiver.method == "get_by_role":
        role_fp = fingerprint.get("role")
        name_fp = fingerprint.get("accessible_name")
        if not role_fp or not name_fp or receiver.role is None or receiver.accessible_name is None:
            return "unresolvable"
        role_ok = _normalized_text(receiver.role) == _normalized_text(role_fp)
        name_ok = _locator_text_matches(receiver.accessible_name, name_fp)
        return "matched" if role_ok and name_ok else "other"
    attr = _SEMANTIC_METHOD_ATTR.get(receiver.method or "")
    if attr is None or receiver.argument is None:
        return "unresolvable"
    fp_val = fingerprint.get(attr)
    if not fp_val:
        return "other" if attr in _probed_attributes(fingerprint) else "unresolvable"
    if receiver.method in _SEMANTIC_EXACT_METHODS:
        return "matched" if receiver.argument.strip() == fp_val.strip() else "other"
    return "matched" if _locator_text_matches(receiver.argument, fp_val) else "other"


def _fingerprint_verdict(receiver: _FillReceiver, fingerprint: Mapping[str, str]) -> _Verdict:
    """Three-valued identity verdict of a fill receiver against a scouted element fingerprint. An empty
    fingerprint (capture degraded) and any attribute the probe never read both resolve 'unresolvable' so
    the caller fails closed; a mismatch, or an attribute the probe read and found absent, earns 'other'."""
    if not fingerprint:
        return "unresolvable"
    if receiver.kind == "css" and receiver.css_selector is not None:
        return _css_fingerprint_verdict(receiver.css_selector, fingerprint)
    if receiver.kind == "semantic":
        return _semantic_fingerprint_verdict(receiver, fingerprint)
    return "unresolvable"


def _best_fingerprint_verdict(
    receiver: _FillReceiver,
    targets: Mapping[str, tuple[str, str]],
    fingerprints_by_selector: Mapping[str, dict[str, str]],
    source_urls_by_selector: Mapping[str, frozenset[str]],
    block_goto_urls: set[str],
) -> tuple[_Verdict, tuple[str, str, str] | None]:
    """Resolve a fill receiver against every scouted credential target reachable from this block. A single
    matched target repairs; otherwise 'other' (provably a different element) beats 'unresolvable' so a
    benign literal is admitted rather than failed closed."""
    best: _Verdict = "unresolvable"
    matched: list[tuple[str, str, str]] = []
    for selector, (credential_id, field) in targets.items():
        if _block_page_incompatible(block_goto_urls, source_urls_by_selector.get(selector, frozenset())):
            continue
        verdict = _fingerprint_verdict(receiver, fingerprints_by_selector.get(selector, {}))
        if verdict == "matched":
            matched.append((selector, credential_id, field))
        elif verdict == "other":
            best = "other"
    if len({(credential_id, field) for _selector, credential_id, field in matched}) > 1:
        # A coarse receiver (e.g. `input[type="password"]`) can match a password and a confirm-password
        # target alike. Repairing to whichever came first would bind the wrong credential, so ambiguity
        # fails closed here exactly as a conflicting scouted selector does in `scouted_credential_targets`.
        return "unresolvable", None
    if matched:
        return "matched", matched[0]
    return best, None


def rebind_scouted_credential_literals(
    workflow_yaml: str | None, scout_trajectory: Sequence[Mapping[str, Any]] | None
) -> CredentialRebindResult:
    """Rewrite raw credential literals in authored code blocks into credential-parameter access.

    Any fill/type whose selector was scouted by `fill_credential_field` is rebound to
    `<credential_param>.<field>` regardless of which author wrote the code, so a leaked literal
    cannot reach persistence.
    """
    empty = CredentialRebindResult(workflow_yaml=workflow_yaml or "", changed=False, rebound=())
    if not workflow_yaml or not workflow_yaml.strip():
        return empty
    targets = scouted_credential_targets(scout_trajectory)
    if not targets:
        return empty

    def _skipped(stage: str) -> CredentialRebindResult:
        return CredentialRebindResult(
            workflow_yaml=workflow_yaml or "",
            changed=False,
            rebound=(),
            skips=tuple(CredentialRebindSkip(stage, selector) for selector in targets),
        )

    source_urls_by_selector = scouted_selector_source_urls(scout_trajectory)
    fingerprints_by_selector = scouted_element_fingerprints(scout_trajectory)
    try:
        parsed = yaml.safe_load(workflow_yaml)
    except yaml.YAMLError:
        return _skipped("yaml-parse")
    if not isinstance(parsed, dict):
        return _skipped("no-definition")
    workflow_definition = parsed.get("workflow_definition")
    if not isinstance(workflow_definition, dict):
        return _skipped("no-definition")
    blocks = workflow_blocks(parsed)
    if not blocks:
        return _skipped("no-blocks")

    parameters = workflow_definition.get("parameters")
    parameters = parameters if isinstance(parameters, list) else []
    used_keys = {str(p.get("key")) for p in parameters if isinstance(p, dict) and p.get("key")}
    assigned: dict[str, str] = {}

    def param_key_for(credential_id: str) -> str:
        if credential_id in assigned:
            return assigned[credential_id]
        key = _existing_param_key(parameters, credential_id) or _mint_param_key(credential_id, used_keys)
        assigned[credential_id] = key
        return key

    all_rebound: list[str] = []
    minted_for: set[str] = set()

    for block in blocks:
        if not isinstance(block, dict):
            continue
        code = block.get("code")
        if not isinstance(code, str) or not code.strip():
            continue
        new_code, rebound, used_params = _rebind_block_code(
            code, targets, param_key_for, source_urls_by_selector, fingerprints_by_selector
        )
        if not rebound:
            continue
        block["code"] = new_code
        all_rebound.extend(rebound)
        parameter_keys = block.get("parameter_keys")
        parameter_keys = list(parameter_keys) if isinstance(parameter_keys, list) else []
        for key in sorted(used_params):
            if key not in parameter_keys:
                parameter_keys.append(key)
        block["parameter_keys"] = parameter_keys
        minted_for.update(used_params)

    for credential_id, _field in targets.values():
        param_key_for(credential_id)
    credential_keys: set[str] = set(credential_param_ids(parameters)) | set(assigned.values())

    authored, skips = _author_missing_obligations(
        blocks, targets, source_urls_by_selector, assigned, minted_for, credential_keys
    )

    residual = tuple(
        _residual_raw_credential_fills(blocks, targets, source_urls_by_selector, assigned, fingerprints_by_selector)
    )

    if not all_rebound and not authored:
        return CredentialRebindResult(
            workflow_yaml=workflow_yaml or "",
            changed=False,
            rebound=(),
            skips=tuple(skips),
            residual_selectors=residual,
        )

    for credential_id, key in assigned.items():
        if key not in minted_for:
            continue
        if _existing_param_key(parameters, credential_id):
            continue
        parameters.append({"key": key, "parameter_type": "credential", "credential_id": credential_id})
    workflow_definition["parameters"] = parameters

    return CredentialRebindResult(
        workflow_yaml=yaml.safe_dump(parsed, sort_keys=False),
        changed=True,
        rebound=tuple(all_rebound),
        authored=tuple(authored),
        skips=tuple(skips),
        residual_selectors=residual,
    )


def _author_missing_obligations(
    blocks: Sequence[Any],
    targets: Mapping[str, tuple[str, str]],
    source_urls_by_selector: Mapping[str, frozenset[str]],
    assigned: Mapping[str, str],
    minted_for: set[str],
    credential_keys: AbstractSet[str],
) -> tuple[list[tuple[str, str]], list[CredentialRebindSkip]]:
    authored: list[tuple[str, str]] = []
    skips: list[CredentialRebindSkip] = []

    def block_infos() -> list[
        tuple[dict[str, Any], set[str], list[tuple[str, ast.AST, ast.Call, ast.stmt | None]], bool]
    ]:
        infos = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            code = block.get("code")
            if not isinstance(code, str) or not code.strip():
                continue
            goto = {_normalize_page_url(url) for url in _GOTO_RE.findall(code)}
            fills, unrecognized_values = _scan_block_fills(code)
            infos.append((block, goto, fills, bool(unrecognized_values)))
        return infos

    for selector, (credential_id, field) in targets.items():
        param_key = assigned[credential_id]
        target_source_urls = source_urls_by_selector.get(selector, frozenset())
        satisfied = False
        unmatched: tuple[dict[str, Any], ast.Call, ast.stmt, ast.AST] | None = None
        inline_literal_present = False
        runtime_reference_present = False
        saw_compatible_block = False
        for block, goto, fills, _unrecognized in block_infos():
            if _block_page_incompatible(goto, target_source_urls):
                continue
            saw_compatible_block = True
            block_code = str(block.get("code") or "")
            for fsel, fval, fnode, fstmt in fills:
                if fsel != selector:
                    continue
                if _value_is_param_field(fval, param_key, field):
                    satisfied = True
                elif _value_is_inline_literal_fill(fval, block_code):
                    inline_literal_present = True
                    if unmatched is None and fstmt is not None:
                        unmatched = (block, fnode, fstmt, fval)
                else:
                    runtime_reference_present = True
        if satisfied:
            continue
        if not saw_compatible_block:
            skips.append(CredentialRebindSkip("page-incompatible", selector))
            continue
        if runtime_reference_present and not inline_literal_present:
            # The selector is already filled with a runtime reference (a foreign credential parameter, a
            # subscript, an identifier bound to a call). It carries no inline secret; leave it as the
            # shipped backstop does rather than author a duplicate or clobber the author's value.
            continue
        if inline_literal_present:
            # Replace the draft's own credential fill with the sanctioned reference. The secret scan does
            # NOT catch the bare `page.fill(sel, "...")` shape, so keeping the raw value would leak it; an
            # unguarded parameter fill is strictly safer (the persist seam refuses an unguarded fill, but a
            # refused draft never persists a secret). Guardedness is not a reason to keep the literal.
            if unmatched is not None:
                block, call, stmt, old_value = unmatched
                code = str(block.get("code") or "")
                locator_expr = _fill_locator_expr(code.splitlines(), call, selector)
                new_code = _replace_fill_statement(code, stmt, locator_expr, param_key, field) if locator_expr else None
                if new_code is not None:
                    # The replaced value may have been an identifier bound to a literal; the now-dead
                    # `var = "<secret>"` assignment would keep the raw credential in the block.
                    old_target = old_value.value if isinstance(old_value, ast.Await) else old_value
                    if isinstance(old_target, ast.Name):
                        new_code = _drop_dead_string_assignment(new_code, old_target.id)
                    block["code"] = new_code
                    parameter_keys = block.get("parameter_keys")
                    parameter_keys = list(parameter_keys) if isinstance(parameter_keys, list) else []
                    if param_key not in parameter_keys:
                        parameter_keys.append(param_key)
                    block["parameter_keys"] = parameter_keys
                    minted_for.add(param_key)
                    authored.append((selector, field))
                    continue
            skips.append(CredentialRebindSkip("literal-unmatched", selector))
            continue

        sibling_fields = {
            sib_selector: sib_field
            for sib_selector, (sib_credential_id, sib_field) in targets.items()
            if sib_selector != selector and sib_credential_id == credential_id
        }
        anchor = None
        for block, goto, fills, has_unrecognized in block_infos():
            if _block_page_incompatible(goto, target_source_urls):
                continue
            sibling = next(
                (
                    (fsel, fval)
                    for fsel, fval, _fnode, _fstmt in fills
                    if fsel in sibling_fields and _value_is_param_field(fval, param_key, sibling_fields[fsel])
                ),
                None,
            )
            if sibling is not None:
                anchor = (block, goto, has_unrecognized, sibling[0])
                break
        if anchor is None:
            skips.append(CredentialRebindSkip("no-anchor", selector))
            continue
        anchor_block, anchor_goto, anchor_has_unrecognized, sibling_selector = anchor
        if anchor_has_unrecognized:
            skips.append(CredentialRebindSkip("unrecognized-fill-shape", selector))
            continue
        if not _has_positive_page_evidence(
            anchor_goto, target_source_urls, source_urls_by_selector.get(sibling_selector, frozenset())
        ):
            skips.append(CredentialRebindSkip("no-page-evidence", selector))
            continue
        new_code = _author_missing_credential_fill(
            str(anchor_block.get("code") or ""), selector, param_key, field, sibling_fields, credential_keys
        )
        if new_code is None:
            skips.append(CredentialRebindSkip("guard-inadmissible", selector))
            continue
        anchor_block["code"] = new_code
        parameter_keys = anchor_block.get("parameter_keys")
        parameter_keys = list(parameter_keys) if isinstance(parameter_keys, list) else []
        if param_key not in parameter_keys:
            parameter_keys.append(param_key)
        anchor_block["parameter_keys"] = parameter_keys
        minted_for.add(param_key)
        authored.append((selector, field))

    return authored, skips
