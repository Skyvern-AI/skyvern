import re

import pytest
from jinja2.exceptions import SecurityError

from skyvern.utils.templating import Constants, get_missing_variables, replace_jinja_reference


@pytest.mark.parametrize(
    "template,data,expected",
    [
        ("", {}, set()),
        ("Hello {{ name }}", {"name": "World"}, set()),
        ("Hello {{ name }}", {"age": 30}, {"name"}),
        ("{{ one }}", {"one": 1, "two": 2}, set()),  # extra vars allowed
        # nested (dotted) variables
        ("{{ user.name }}", {"user": {"name": "Alice"}}, set()),
        ("{{ user.name }}", {"user": {"age": 30}}, {"user.name"}),
        # list access
        ("{{ items[0] }}", {}, {"items"}),
        ("{{ items[0] }}", {"items": [1, 2, 3]}, set()),
        ("{{ items[0] }}", {"items": []}, {"items[0]"}),
        # deeply nested lists and dicts
        ("{{ data.users[0].name }}", {"data": {"users": [{"name": "Bob"}]}}, set()),
        ("{{ data.users[0].name }}", {"data": {"users": [{}]}}, {"data.users[0].name"}),
        ("{{ data.users[0].name }}", {"data": {}}, {"data.users[0].name"}),
    ],
)
def test_get_missing_variables(template, data, expected):
    missing_vars = get_missing_variables(template, data)
    assert missing_vars == expected


@pytest.mark.parametrize(
    "template,expected",
    [
        ("{{ var }}", {"var"}),
        ("{{ var.attr }}", {"var.attr"}),
        ("{{ var[0] }}", {"var[0]"}),
        ("{{ var['key'] }}", {"var['key']"}),
        ('{{ var["key"] }}', {'var["key"]'}),
        ("{{ var.attr[0] }}", {"var.attr[0]"}),
        ("No variables here", set()),
        ("{{ var1 }} and {{ var2.attr }}", {"var1", "var2.attr"}),
    ],
)
def test_regex_missing_variable_pattern(template, expected):
    matches = set(re.findall(Constants.MissingVariablePattern, template))
    assert matches == expected


@pytest.mark.parametrize(
    "gadget",
    [
        "{{ ''.__class__.__mro__[1].__subclasses__() }}",
        "{{ [].__class__.__base__ }}",
        "{% for cls in ().__class__.__bases__ %}{{ cls }}{% endfor %}",
    ],
)
def test_get_missing_variables_never_renders_gadgets_unsandboxed(gadget):
    with pytest.raises(SecurityError):
        get_missing_variables(gadget, {})


@pytest.mark.parametrize(
    "text,old_key,new_key,expected",
    [
        # head-of-expression references (already worked before the fix)
        ("{{ old }}", "old", "new", "{{ new }}"),
        ("{{  old  }}", "old", "new", "{{  new  }}"),
        ("{{ old.field }}", "old", "new", "{{ new.field }}"),
        ("{{ old | default(1) }}", "old", "new", "{{ new | default(1) }}"),
        ("{{ old[0] }}", "old", "new", "{{ new[0] }}"),
        # mid-expression references (issue #7559): while-loop Condition shape from the docs
        ("{{ current_index < old }}", "old", "new", "{{ current_index < new }}"),
        ("{{ old + 1 }}", "old", "new", "{{ new + 1 }}"),
        ("{{ other and old }}", "old", "new", "{{ other and new }}"),
        # {% %} statement references (issue #7559)
        ("{% if count > old %}x{% endif %}", "old", "new", "{% if count > new %}x{% endif %}"),
        ("{% for item in old %}{{ item }}{% endfor %}", "old", "new", "{% for item in new %}{{ item }}{% endfor %}"),
        ("{% set total = old * 2 %}", "old", "new", "{% set total = new * 2 %}"),
        # identifiers that merely contain the key must be left alone
        ("{{ old_other }}", "old", "new", "{{ old_other }}"),
        ("{{ other_old }}", "old", "new", "{{ other_old }}"),
        ("{{ old2 }}", "old", "new", "{{ old2 }}"),
        # attribute access on another root is not the renamed parameter
        ("{{ data.old }}", "old", "new", "{{ data.old }}"),
        # quoted strings are literals, not references
        ("{{ 'old' }}", "old", "new", "{{ 'old' }}"),
        ('{{ "old" }}', "old", "new", '{{ "old" }}'),
        ("{{ data['old'] }}", "old", "new", "{{ data['old'] }}"),
        # jinja comments are not statements
        ("{# old #}", "old", "new", "{# old #}"),
        # text outside jinja tags is never touched
        ("old", "old", "new", "old"),
        ("the old value", "old", "new", "the old value"),
        # renamed keys can contain characters that are not valid in identifiers
        ("{{ max-attempts }}", "max-attempts", "max_attempts", "{{ max_attempts }}"),
        ("{{ i < max-attempts }}", "max-attempts", "max_attempts", "{{ i < max_attempts }}"),
        ("{% if max-attempts %}x{% endif %}", "max-attempts", "max_attempts", "{% if max_attempts %}x{% endif %}"),
        ("{{ max-attempts2 }}", "max-attempts", "max_attempts", "{{ max-attempts2 }}"),
        # multiple tags in one template
        (
            "{{ old }} and {% if old %}y{% endif %} and {{ old }}",
            "old",
            "new",
            "{{ new }} and {% if new %}y{% endif %} and {{ new }}",
        ),
    ],
)
def test_replace_jinja_reference(text, old_key, new_key, expected):
    assert replace_jinja_reference(text, old_key, new_key) == expected
