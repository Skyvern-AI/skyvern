import pytest

from skyvern.services.script_service import render_list


class TestRenderListBlocksExploits:
    """render_list() must reject all non-literal expressions."""

    @pytest.mark.parametrize(
        "payload,description",
        [
            ("__import__('os').system('id')", "OS command execution"),
            ("__import__('os').popen('cat /etc/passwd').read()", "File read via popen"),
            ("__import__('subprocess').check_output(['whoami'])", "Subprocess execution"),
            ("__import__('os').environ", "Environment variable access"),
            ("open('/etc/passwd').read()", "Direct file read"),
            ("(lambda: __import__('os').system('id'))()", "Lambda-wrapped execution"),
            ("eval('1+1')", "Nested eval"),
            ("exec('import os')", "exec() call"),
            (
                "[x for x in ().__class__.__bases__[0].__subclasses__() if 'warning' in str(x)][0]()._module.__builtins__['__import__']('os').system('id')",
                "Class hierarchy escape",
            ),
        ],
    )
    def test_rejects_malicious_input(self, payload: str, description: str) -> None:
        with pytest.raises((ValueError, SyntaxError, TypeError)):
            render_list(payload)

    def test_rejects_function_calls(self) -> None:
        with pytest.raises((ValueError, SyntaxError)):
            render_list("print('hello')")


class TestRenderListNormalUsage:
    """Legitimate render_list() inputs must still parse correctly."""

    def test_list_of_strings(self) -> None:
        result = render_list("['[email protected]', '[email protected]']")
        assert result == ["[email protected]", "[email protected]"]

    def test_single_string_wrapped_in_list(self) -> None:
        result = render_list("'[email protected]'")
        assert result == ["[email protected]"]

    def test_list_of_numbers(self) -> None:
        result = render_list("[1, 2, 3]")
        assert result == [1, 2, 3]

    def test_empty_list(self) -> None:
        result = render_list("[]")
        assert result == []

    def test_nested_list(self) -> None:
        result = render_list("[['a', 'b'], ['c']]")
        assert result == [["a", "b"], ["c"]]

    def test_jinja_template_rendering(self) -> None:
        result = render_list("['{{ name }}']", data={"name": "alice"})
        assert result == ["alice"]

    def test_jinja_template_resolves_to_list_literal(self) -> None:
        result = render_list("{{ items }}", data={"items": "['a', 'b']"})
        assert result == ["a", "b"]
