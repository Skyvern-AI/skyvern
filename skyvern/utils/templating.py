import re
from typing import Any

from jinja2 import StrictUndefined, UndefinedError, meta, nodes
from jinja2.sandbox import SandboxedEnvironment


class Constants:
    MissingVariablePattern = var_pattern = r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_.\[\]'\"]*)\s*\}\}"


def replace_jinja_reference(text: str, old_key: str, new_key: str) -> str:
    """Replaces jinja-style references in a string.

    Handles patterns like {{oldKey}}, {{oldKey.field}}, {{oldKey | filter}}, {{oldKey[0]}}

    Args:
        text: The text to search in
        old_key: The key to replace (without braces)
        new_key: The new key to use (without braces)

    Returns:
        The text with references replaced
    """
    # Match {{oldKey}} or {{oldKey.something}} or {{oldKey | filter}} or {{oldKey[0]}} etc.
    # Use negative lookahead to ensure key is not followed by identifier characters,
    # which prevents matching {{keyOther}} when searching for {{key}}
    # Capture whitespace after {{ to preserve formatting (e.g., "{{ key }}" stays "{{ newKey }}")
    escaped_old_key = re.escape(old_key)
    pattern = rf"\{{\{{(\s*){escaped_old_key}(?![a-zA-Z0-9_])"
    return re.sub(pattern, rf"{{{{\1{new_key}", text)


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
