import re

from jinja2 import StrictUndefined, UndefinedError, meta
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
    span ended, so malformed input (e.g. thousands of unmatched braces) stays linear
    instead of rescanning to the end from every opener.

    Args:
        text: The text to search in
        old_key: The key to replace (without braces)
        new_key: The new key to use (without braces)

    Returns:
        The text with references replaced
    """
    escaped_old_key = re.escape(old_key)
    # An unclosed "{{ oldKey" has always been rewritten at the leading position; spans
    # that never close fall back to this legacy leading-position rewrite. The pattern is
    # anchored on the literal "{{" with no wildcards, so it scans linearly.
    leading_pattern = re.compile(rf"\{{\{{(\s*){escaped_old_key}(?![a-zA-Z0-9_])")

    parts: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        # Find the next opener; -1 sentinels normalized to n so min() picks the earliest.
        expr_start = text.find("{{", i)
        stmt_start = text.find("{%", i)
        starts = [pos for pos in (expr_start, stmt_start) if pos != -1]
        if not starts:
            parts.append(text[i:])
            break
        start = min(starts)
        parts.append(text[i:start])
        closer = "}}" if text.startswith("{{", start) else "%}"
        end = text.find(closer, start + 2)
        if end == -1:
            # Unclosed span: the remainder gets only the legacy leading-position rewrite.
            parts.append(leading_pattern.sub(lambda m: "{{" + m.group(1) + new_key, text[start:]))
            break
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
