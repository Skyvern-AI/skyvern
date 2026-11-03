import pytest
from jinja2.exceptions import SecurityError

from skyvern.utils.templating import get_missing_variables


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
