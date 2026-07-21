import re

from jinja2 import StrictUndefined, UndefinedError, meta
from jinja2.sandbox import SandboxedEnvironment


class Constants:
    MissingVariablePattern = var_pattern = r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_.\[\]'\"]*)\s*\}\}"


# Closed Jinja expression/statement spans. DOTALL because expressions may span lines.
_JINJA_SPAN_PATTERN = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)


def replace_jinja_reference(text: str, old_key: str, new_key: str) -> str:
    """Replaces jinja-style references in a string.

    Rewrites the key wherever it appears as a full token inside {{ ... }} expressions or
    {% ... %} statements: {{oldKey}}, {{oldKey.field}}, {{oldKey | filter}}, {{oldKey[0]}},
    {{ other < oldKey }}, {% if oldKey %}, {% for x in oldKey %}.

    Left untouched: occurrences outside Jinja delimiters, attribute accesses (foo.oldKey),
    quoted string literals ('oldKey'), and longer identifiers that merely contain the key
    (oldKeyExtended).

    Args:
        text: The text to search in
        old_key: The key to replace (without braces)
        new_key: The new key to use (without braces)

    Returns:
        The text with references replaced
    """
    escaped_old_key = re.escape(old_key)
    # A full-token occurrence: not preceded by an identifier character, a dot (attribute
    # access), or a quote (string literal), and not followed by an identifier character or
    # a quote. A trailing dot/bracket stays allowed so {{oldKey.field}} and {{oldKey[0]}}
    # keep their access path with only the root renamed.
    token_pattern = re.compile(rf"(?<![a-zA-Z0-9_.'\"]){escaped_old_key}(?![a-zA-Z0-9_'\"])")

    def _rewrite_span(span: re.Match[str]) -> str:
        return token_pattern.sub(lambda _: new_key, span.group(0))

    text = _JINJA_SPAN_PATTERN.sub(_rewrite_span, text)

    # An unclosed "{{ oldKey" has always been rewritten at the leading position; the span
    # pattern above can't see it, so keep the legacy leading-position rewrite as a fallback.
    leading_pattern = rf"\{{\{{(\s*){escaped_old_key}(?![a-zA-Z0-9_])"
    return re.sub(leading_pattern, lambda m: "{{" + m.group(1) + new_key, text)


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
