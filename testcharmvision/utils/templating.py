import re

from jinja2 import Environment, StrictUndefined, UndefinedError, meta


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
    # quick check - catch top-level undefineds
    env = Environment(undefined=StrictUndefined)
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
