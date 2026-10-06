import json
import keyword
import re
from typing import Any, Callable

from jinja2 import StrictUndefined, UndefinedError, meta, nodes
from jinja2.sandbox import SandboxedEnvironment


class Constants:
    MissingVariablePattern = var_pattern = r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_.\[\]'\"]*)\s*\}\}"


_JINJA_TAG_CLOSERS = {"{%": "%}", "{#": "#}"}


def _jinja_tag_spans(code: str) -> list[tuple[int, int]]:
    """Leftmost non-overlapping `{% ... %}` / `{# ... #}` spans, found in one forward pass.

    Once an opener has no closer after it, no later opener of that kind can close either, so that kind
    is dropped; a regex search would instead rescan to the end from every later opener (quadratic)."""
    spans: list[tuple[int, int]] = []
    next_opener = {opener: code.find(opener) for opener in _JINJA_TAG_CLOSERS}
    position = 0
    while True:
        for opener, index in list(next_opener.items()):
            if index != -1 and index < position:
                next_opener[opener] = code.find(opener, position)
        open_kinds = {opener: index for opener, index in next_opener.items() if index != -1}
        if not open_kinds:
            return spans
        opener = min(open_kinds, key=open_kinds.__getitem__)
        start = open_kinds[opener]
        end = code.find(_JINJA_TAG_CLOSERS[opener], start + len(opener))
        if end == -1:
            del next_opener[opener]
            continue
        position = end + len(_JINJA_TAG_CLOSERS[opener])
        spans.append((start, position))


def _replace_jinja_tags(code: str, replacement: Callable[[str], str]) -> str:
    parts: list[str] = []
    cursor = 0
    for start, end in _jinja_tag_spans(code):
        parts.append(code[cursor:start])
        parts.append(replacement(code[start:end]))
        cursor = end
    parts.append(code[cursor:])
    return "".join(parts)


def mask_jinja_control_blocks(code: str) -> str:
    """Replace each `{% ... %}` block and `{# ... #}` comment in Python source with comment lines, keeping every line number."""
    return _replace_jinja_tags(code, lambda tag: "\n".join("# __JINJA_BLOCK__" for _ in range(tag.count("\n") + 1)))


def strip_jinja_control_blocks(code: str) -> str:
    """Remove each `{% ... %}` block and `{# ... #}` comment from Python source, keeping its newlines so line numbers hold."""
    return _replace_jinja_tags(code, lambda tag: "\n" * tag.count("\n"))


def replace_jinja_reference(text: str, old_key: str, new_key: str) -> str:
    """Replaces jinja-style references in a string.

    Handles references anywhere inside Jinja delimiters: {{oldKey}},
    {{oldKey.field}}, {{oldKey | filter}}, {{oldKey[0]}}, mid-expression uses
    like {{ other < oldKey }}, and statement blocks like {% if oldKey %}.
    Quoted string literals inside an expression are left alone.

    Args:
        text: The text to search in
        old_key: The key to replace (without braces)
        new_key: The new key to use (without braces)

    Returns:
        The text with references replaced
    """
    escaped_old_key = re.escape(old_key)
    # Match the whole identifier on both sides so {{keyOther}} and {{my_key}}
    # stay untouched when searching for {{key}}.
    word = rf"(?<![a-zA-Z0-9_]){escaped_old_key}(?![a-zA-Z0-9_])"
    # Only rewrite inside Jinja delimiters; the rest of the text is literal.
    segment_pattern = re.compile(r"\{\{.*?\}\}|{%.*?%}", re.DOTALL)
    quoted_pattern = re.compile(r"('[^']*'|\"[^\"]*\")")

    def _rewrite_segment(segment_match: re.Match[str]) -> str:
        segment = segment_match.group(0)
        parts = quoted_pattern.split(segment)
        return "".join(part if index % 2 == 1 else re.sub(word, new_key, part) for index, part in enumerate(parts))

    return segment_pattern.sub(_rewrite_segment, text)


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
MAX_AVAILABLE_PATH_LENGTH = 512
_JINJA_RESERVED = frozenset(word.lower() for word in keyword.kwlist) | {"true", "false", "none"}


def _extend_path(path: str, segment: str | int) -> str | None:
    """``path`` extended by one segment, written so Jinja resolves it back to that segment, or None
    when no renderable form exists: an over-long key, or a non-identifier with no root to subscript.
    A segment that also names a dict attribute (``items``, ``keys``) is subscripted, because dotted
    access would resolve to the bound method instead of the value."""
    if isinstance(segment, int):
        return f"{path}[{segment}]" if path else None
    if len(segment) > MAX_AVAILABLE_KEY_LENGTH:
        return None
    if segment.isidentifier() and segment.lower() not in _JINJA_RESERVED:
        if not path:
            return segment
        # hasattr applies to dotted access on a dict, not to a top-level binding of the same name.
        if not hasattr(dict, segment):
            return f"{path}.{segment}"
    return f"{path}[{json.dumps(segment)}]" if path else None


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


def _descend(data: dict[str, Any], root: str, segments: list[str | int]) -> tuple[str, Any]:
    current: Any = data
    path = ""
    for segment in [root, *segments]:
        if isinstance(current, dict) and segment in current:
            descended = current[segment]
        elif isinstance(current, list) and isinstance(segment, int) and -len(current) <= segment < len(current):
            descended = current[segment]
        else:
            return path, current
        extended = _extend_path(path, segment)
        if extended is None:
            return path, current
        path, current = extended, descended
    return path, current


def get_available_keys(template_source: str, template_data: dict[str, Any]) -> list[str]:
    """Reference paths a failing reference in ``template_source`` could have used: one path per key
    of the dict each dotted chain landed on, then the top-level bindings, each written so that
    pasting it into ``{{ }}`` resolves to that key's value. A bare key name is not published: the
    reader cannot tell it from a top-level binding, and pasting it renders nothing. Values are never
    included, but a key of a parsed file or an extracted object is itself run data, so the result is
    bounded in count and in path length rather than treated as free of customer content."""
    reachable: set[str] = set()
    roots: set[str] = set()

    def add(into: set[str], entry: str | None) -> None:
        if entry and len(entry) <= MAX_AVAILABLE_PATH_LENGTH:
            into.add(entry)

    def published() -> list[str]:
        # Paths under the failing reference answer it; top-level names only say what else exists.
        return (sorted(reachable) + sorted(roots - reachable))[:MAX_AVAILABLE_KEYS]

    try:
        for key in template_data:
            if isinstance(key, str):
                add(roots, _extend_path("", key))
        ast = SandboxedEnvironment().parse(template_source)
        chains = list(ast.find_all((nodes.Getattr, nodes.Getitem)))
        nested = {id(chain.node) for chain in chains}
        for chain in chains:
            if id(chain) in nested:
                continue
            flattened = _flatten_reference_chain(chain)
            if flattened is None:
                continue
            path, resolved = _descend(template_data, *flattened)
            if path and isinstance(resolved, dict):
                for key in resolved:
                    # An int key is subscripted as an int; coercing it to a string key would not resolve.
                    if isinstance(key, str) or (isinstance(key, int) and not isinstance(key, bool)):
                        add(reachable, _extend_path(path, key))
    except Exception:
        return published()
    return published()
