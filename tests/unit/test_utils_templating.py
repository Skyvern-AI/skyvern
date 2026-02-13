import re

import pytest

from skyvern.utils.templating import Constants, get_missing_variables


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
