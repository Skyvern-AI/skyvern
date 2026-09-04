import re
from typing import Any

from jinja2 import StrictUndefined, UndefinedError, meta, nodes
from jinja2.sandbox import SandboxedEnvironment


class Constants:
    MissingVariablePattern = var_pattern = r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_.\[\]'\"]*)\s*\}\}"


# Characters that may precede a full-token occurrence of a key (identifier chars would
# make it a longer identifier; a dot would make it an attribute access like foo.key).
_TOKEN_BOUNDARY_BEFORE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.")
# Characters that may not follow a full-token occurrence (identifier continuation).
_TOKEN_BOUNDARY_AFTER = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def _rewrite_span_tokens(span: str, old_key: str, new_key: str) -> str:
    """Rewrites full-token occurrences of old_key in one Jinja span, skipping string literals.

    Single left-to-right pass. Quoted regions ('...' or "...", honoring backslash escapes)
    are copied verbatim, so a key embedded anywhere inside a string literal is never touched.
    """
    parts: list[str] = []
    i = 0
    n = len(span)
    key_len = len(old_key)
    while i < n:
        if span.startswith(old_key, i):
            prev = span[i - 1] if i > 0 else ""
            nxt = span[i + key_len] if i + key_len < n else ""
            if (not prev or prev not in _TOKEN_BOUNDARY_BEFORE) and (not nxt or nxt not in _TOKEN_BOUNDARY_AFTER):
                parts.append(new_key)
                i += key_len
                continue
        char = span[i]
        parts.append(char)
        i += 1
        if char in "'\"":
            # Copy the whole string literal verbatim (backslash escapes included) so
            # embedded occurrences of the key are never rewritten.
            quote = char
            while i < n:
                literal_char = span[i]
                parts.append(literal_char)
                i += 1
                if literal_char == "\\" and i < n:
                    parts.append(span[i])
                    i += 1
                elif literal_char == quote:
                    break
    return "".join(parts)


def replace_jinja_reference(text: str, old_key: str, new_key: str) -> str:
    """Replaces jinja-style references in a string.

    Rewrites the key wherever it appears as a full token inside {{ ... }} expressions or
    {% ... %} statements: {{oldKey}}, {{oldKey.field}}, {{oldKey | filter}}, {{oldKey[0]}},
    {{ other < oldKey }}, {% if oldKey %}, {% for x in oldKey %}.

    Left untouched: occurrences outside Jinja delimiters, attribute accesses (foo.oldKey),
    anything inside quoted string literals ('...oldKey...'), and longer identifiers that
    merely contain the key (oldKeyExtended).

    The scan is a single left-to-right pass: each span search resumes where the previous
    span ended, and an unclosed opener is stepped over after its leading-position rewrite,
    so malformed input (e.g. thousands of unmatched braces) stays linear while well-formed
    spans after an unclosed opener are still fully rewritten.

    Args:
        text: The text to search in
        old_key: The key to replace (without braces)
        new_key: The new key to use (without braces)

    Returns:
        The text with references replaced
    """
    escaped_old_key = re.escape(old_key)
    # An unclosed "{{ oldKey" has always been rewritten at the leading position; openers
    # that never close get this legacy leading-position rewrite. The pattern is anchored
    # on the literal "{{" with no wildcards, so it scans linearly.
    leading_pattern = re.compile(rf"\{{\{{(\s*){escaped_old_key}(?![a-zA-Z0-9_])")

    parts: list[str] = []
    i = 0
    n = len(text)
    # Opener positions are cached until the scan passes them, and a closer type known to
    # be absent from the rest of the text is never searched for again, so every find()
    # covers a distinct stretch of input — the scan stays linear even when thousands of
    # openers never close.
    expr_start = text.find("{{")
    stmt_start = text.find("{%")
    have_expr_closer = True
    have_stmt_closer = True
    while i < n:
        if expr_start != -1 and expr_start < i:
            expr_start = text.find("{{", i)
        if stmt_start != -1 and stmt_start < i:
            stmt_start = text.find("{%", i)
        starts = [pos for pos in (expr_start, stmt_start) if pos != -1]
        if not starts:
            parts.append(text[i:])
            break
        start = min(starts)
        parts.append(text[i:start])
        if text.startswith("{{", start):
            end = text.find("}}", start + 2) if have_expr_closer else -1
            have_expr_closer = end != -1
        else:
            end = text.find("%}", start + 2) if have_stmt_closer else -1
            have_stmt_closer = end != -1
        if end == -1:
            # Unclosed opener: apply the legacy leading-position rewrite at this opener
            # only, then keep scanning — later well-formed spans still get full coverage.
            head = leading_pattern.match(text, start)
            if head is not None:
                parts.append("{{" + head.group(1) + new_key)
                i = head.end()
            else:
                parts.append(text[start : start + 2])
                i = start + 2
            continue
        span_end = end + 2
        parts.append(_rewrite_span_tokens(text[start:span_end], old_key, new_key))
        i = span_end
    return "".join(parts)


def get_missing_variables(template_source: str, template_data: dict) -> set[str]:
    # quick check - catch top-level undefineds. Sandboxed so that rendering
    # untrusted source below cannot reach attribute-access SSTI gadgets
    # (e.g. {{ ''.__class__.__mro__ }}) — SandboxedEnvironment raises SecurityError.
    env = SandboxedEnvironment(undefined=StrictUndefined)
    ast = env.parse(template_source)
    undeclared_vars = meta.find_undeclared_variables(ast)
    missing_vars = undeclared_vars - set(template_data.keys())

    # nested undefined won't be caught; let's check for those
    if not missing_vars:
        # try rendering to catch nested undefineds (dotted attributes, list/dict access)
        try:
            template = env.from_string(template_source)
            template.render(template_data)
        except UndefinedError:
            # matches: {{ var }}, {{ var.attr }}, {{ var[0] }}, {{ var['key'] }}, {{ var.attr[0] }}
            matches = re.findall(Constants.MissingVariablePattern, template_source)

            for match in matches:
                root = match.split("[")[0].split(".")[0]

                # just check if the 'root' of the variable exists in the provided data
                # if it does, add the whole match as missing
                if root in template_data:
                    missing_vars.add(match)

            if not missing_vars:
                raise  # re-raise if we couldn't determine missing vars

    return missing_vars


MAX_AVAILABLE_KEYS = 200
MAX_AVAILABLE_KEY_LENGTH = 128


def _flatten_reference_chain(node: nodes.Node) -> tuple[str, list[str | int]] | None:
    segments: list[str | int] = []
    while isinstance(node, (nodes.Getattr, nodes.Getitem)):
        if isinstance(node, nodes.Getattr):
            segments.append(node.attr)
        else:
            arg = node.arg
            if not isinstance(arg, nodes.Const) or not isinstance(arg.value, (str, int)):
                return None
            segments.append(arg.value)
        node = node.node
    if not isinstance(node, nodes.Name):
        return None
    segments.reverse()
    return node.name, segments


def _descend(data: dict[str, Any], root: str, segments: list[str | int]) -> Any:
    current: Any = data
    for segment in [root, *segments]:
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        elif isinstance(current, list) and isinstance(segment, int) and -len(current) <= segment < len(current):
            current = current[segment]
        else:
            return current
    return current


def get_available_keys(template_source: str, template_data: dict[str, Any]) -> list[str]:
    """Key names that a failing reference in ``template_source`` could have used: the keys of the
    deepest dict each dotted chain resolved to, plus the top-level bindings. Values are never
    included, but a key of a parsed file or an extracted object is itself run data, so the result
    is bounded in count and per-key length rather than treated as free of customer content."""
    keys: set[str] = set()
    try:
        keys.update(str(key)[:MAX_AVAILABLE_KEY_LENGTH] for key in template_data)
        ast = SandboxedEnvironment().parse(template_source)
        chains = list(ast.find_all((nodes.Getattr, nodes.Getitem)))
        nested = {id(chain.node) for chain in chains}
        for chain in chains:
            if id(chain) in nested:
                continue
            flattened = _flatten_reference_chain(chain)
            if flattened is None:
                continue
            resolved = _descend(template_data, *flattened)
            if isinstance(resolved, dict):
                keys.update(str(key)[:MAX_AVAILABLE_KEY_LENGTH] for key in resolved)
    except Exception:
        return sorted(keys)[:MAX_AVAILABLE_KEYS]
    return sorted(keys)[:MAX_AVAILABLE_KEYS]
