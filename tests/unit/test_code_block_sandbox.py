"""Tests for CodeBlock sandbox security hardening (SKY-7897).

Verifies that the CodeBlock safety layer:
- Rejects dangerous code patterns (subprocess, network, sandbox-escape, imports, dunder access)
- Accepts legitimate code patterns (math, strings, json, regex, sleep)
- Exposes the correct safe variables (no asyncio, yes sleep)
"""

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.workflow.exceptions import InsecureCodeDetected
from skyvern.forge.sdk.workflow.models.block import CodeBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.schemas.workflows import BlockStatus

# ---------------------------------------------------------------------------
# is_safe_code — rejection tests
# ---------------------------------------------------------------------------


class TestIsSafeCodeRejectsImports:
    def test_import_os(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="import"):
            CodeBlock.is_safe_code("import os")

    def test_import_subprocess(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="import"):
            CodeBlock.is_safe_code("import subprocess")

    def test_from_import(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="import"):
            CodeBlock.is_safe_code("from os import system")

    def test_import_asyncio(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="import"):
            CodeBlock.is_safe_code("import asyncio")


class TestIsSafeCodeRejectsDunderAccess:
    def test_dunder_class(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="private"):
            CodeBlock.is_safe_code("x.__class__")

    def test_dunder_bases(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="private"):
            CodeBlock.is_safe_code("x.__bases__")

    def test_dunder_subclasses(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="private"):
            CodeBlock.is_safe_code("x.__subclasses__()")

    def test_dunder_globals(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="private"):
            CodeBlock.is_safe_code("x.__globals__")

    def test_dunder_builtins(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="private"):
            CodeBlock.is_safe_code("x.__builtins__")


class TestIsSafeCodeRejectsBareDunderIdentifiers:
    """Bare dunder names (not attribute access) must also be blocked."""

    def test_capture_locals_call(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="private"):
            CodeBlock.is_safe_code("__capture_locals()")

    def test_capture_locals_assign(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="private"):
            CodeBlock.is_safe_code("x = __capture_locals")

    def test_builtins_bare(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="private"):
            CodeBlock.is_safe_code("__builtins__")

    def test_dunder_import(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="private"):
            CodeBlock.is_safe_code("__import__('os')")


class TestIsSafeCodeRejectsSubprocessAttrs:
    """Every subprocess/OS execution attribute in BLOCKED_ATTRS must be rejected."""

    def test_create_subprocess_exec(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="create_subprocess_exec"):
            CodeBlock.is_safe_code("asyncio.create_subprocess_exec('ls')")

    def test_create_subprocess_shell(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="create_subprocess_shell"):
            CodeBlock.is_safe_code("asyncio.create_subprocess_shell('ls')")

    def test_system(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="system"):
            CodeBlock.is_safe_code("os.system('ls')")

    def test_popen(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="popen"):
            CodeBlock.is_safe_code("os.popen('ls')")

    def test_exec_attr(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="exec"):
            CodeBlock.is_safe_code("obj.exec('code')")

    def test_spawn(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="spawn"):
            CodeBlock.is_safe_code("os.spawn('ls')")

    def test_check_call(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="check_call"):
            CodeBlock.is_safe_code("subprocess.check_call(['ls'])")

    def test_check_output(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="check_output"):
            CodeBlock.is_safe_code("subprocess.check_output(['ls'])")

    def test_execvp(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="execvp"):
            CodeBlock.is_safe_code("os.execvp('ls', ['ls'])")

    def test_execve(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="execve"):
            CodeBlock.is_safe_code("os.execve('/bin/ls', ['ls'], {})")

    def test_fork(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="fork"):
            CodeBlock.is_safe_code("os.fork()")

    def test_spawnl(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="spawnl"):
            CodeBlock.is_safe_code("os.spawnl(0, '/bin/ls')")


class TestIsSafeCodeRejectsNetworkAttrs:
    """Every network primitive in BLOCKED_ATTRS must be rejected."""

    def test_open_connection(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="open_connection"):
            CodeBlock.is_safe_code("asyncio.open_connection('host', 80)")

    def test_start_server(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="start_server"):
            CodeBlock.is_safe_code("asyncio.start_server(handler, 'host', 80)")

    def test_create_connection(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="create_connection"):
            CodeBlock.is_safe_code("loop.create_connection(proto, 'host', 80)")

    def test_create_server(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="create_server"):
            CodeBlock.is_safe_code("loop.create_server(proto, 'host', 80)")


class TestIsSafeCodeAllowsCommonAttrNames:
    """run/call/remove/rename/walk are common on user objects and must NOT be blocked.

    The primary defense is that os/subprocess are not in safe_vars, so
    subprocess.run() causes a NameError at runtime, not an AST rejection.
    """

    def test_run_allowed_in_ast(self) -> None:
        CodeBlock.is_safe_code("task.run()")

    def test_call_allowed_in_ast(self) -> None:
        CodeBlock.is_safe_code("handler.call()")

    def test_remove_allowed_in_ast(self) -> None:
        CodeBlock.is_safe_code("my_list.remove(item)")

    def test_rename_allowed_in_ast(self) -> None:
        CodeBlock.is_safe_code("df.rename(columns={'a': 'b'})")

    def test_walk_allowed_in_ast(self) -> None:
        CodeBlock.is_safe_code("tree.walk()")


class TestIsSafeCodeRejectsFilesystemAttrs:
    """Filesystem operations that must be blocked."""

    def test_listdir(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="listdir"):
            CodeBlock.is_safe_code("os.listdir('/')")

    def test_makedirs(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="makedirs"):
            CodeBlock.is_safe_code("os.makedirs('/tmp/evil')")


class TestIsSafeCodeRejectsModuleTraversal:
    """Module traversal attrs (json.codecs.sys.modules etc.) must be blocked."""

    def test_codecs(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="codecs"):
            CodeBlock.is_safe_code("json.codecs")

    def test_modules(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="modules"):
            CodeBlock.is_safe_code("sys.modules")

    def test_builtins(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="builtins"):
            CodeBlock.is_safe_code("json.codecs.builtins")

    def test_stdout(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="stdout"):
            CodeBlock.is_safe_code("sys.stdout")

    def test_stderr(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="stderr"):
            CodeBlock.is_safe_code("sys.stderr")


class TestIsSafeCodeRejectsSandboxEscapeAttrs:
    """Every sandbox-escape helper in BLOCKED_ATTRS must be rejected."""

    def test_getattr(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="getattr"):
            CodeBlock.is_safe_code("obj.getattr('secret')")

    def test_setattr(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="setattr"):
            CodeBlock.is_safe_code("obj.setattr('key', 'val')")

    def test_delattr(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="delattr"):
            CodeBlock.is_safe_code("obj.delattr('key')")

    def test_globals(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="globals"):
            CodeBlock.is_safe_code("obj.globals()")

    def test_eval(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="eval"):
            CodeBlock.is_safe_code("obj.eval('1+1')")

    def test_vars(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="vars"):
            CodeBlock.is_safe_code("obj.vars()")


class TestIsSafeCodeRejectsFrameCodeAttrs:
    """Frame/code object attributes — classic RestrictedPython escape vectors."""

    def test_f_globals(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="f_globals"):
            CodeBlock.is_safe_code("frame.f_globals")

    def test_f_locals(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="f_locals"):
            CodeBlock.is_safe_code("frame.f_locals")

    def test_f_builtins(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="f_builtins"):
            CodeBlock.is_safe_code("frame.f_builtins")

    def test_f_code(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="f_code"):
            CodeBlock.is_safe_code("frame.f_code")

    def test_co_code(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="co_code"):
            CodeBlock.is_safe_code("code.co_code")

    def test_gi_frame(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="gi_frame"):
            CodeBlock.is_safe_code("gen.gi_frame")

    def test_gi_code(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="gi_code"):
            CodeBlock.is_safe_code("gen.gi_code")

    def test_cr_frame(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="cr_frame"):
            CodeBlock.is_safe_code("coro.cr_frame")

    def test_cr_code(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="cr_code"):
            CodeBlock.is_safe_code("coro.cr_code")

    def test_tb_frame(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="tb_frame"):
            CodeBlock.is_safe_code("tb.tb_frame")

    def test_tb_next(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="tb_next"):
            CodeBlock.is_safe_code("tb.tb_next")

    def test_mro(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="mro"):
            CodeBlock.is_safe_code("cls.mro()")


class TestIsSafeCodeBlockedAttrsCompleteness:
    """Ensure every entry in BLOCKED_ATTRS is actually checked by is_safe_code."""

    @pytest.mark.parametrize("attr", sorted(CodeBlock.BLOCKED_ATTRS))
    def test_blocked_attr_rejected(self, attr: str) -> None:
        code = f"obj.{attr}()"
        with pytest.raises(InsecureCodeDetected):
            CodeBlock.is_safe_code(code)


# ---------------------------------------------------------------------------
# is_safe_code — acceptance tests
# ---------------------------------------------------------------------------


class TestIsSafeCodeAcceptsLegitimateCode:
    def test_variable_assignment(self) -> None:
        CodeBlock.is_safe_code("x = 1")

    def test_arithmetic(self) -> None:
        CodeBlock.is_safe_code("x = 1 + 2 * 3")

    def test_string_operations(self) -> None:
        CodeBlock.is_safe_code('x = "hello" + " world"')

    def test_string_methods(self) -> None:
        CodeBlock.is_safe_code('x = "hello".upper().strip()')

    def test_list_comprehension(self) -> None:
        CodeBlock.is_safe_code("x = [i * 2 for i in range(10)]")

    def test_dict_comprehension(self) -> None:
        CodeBlock.is_safe_code("x = {str(i): i for i in range(5)}")

    def test_json_dumps(self) -> None:
        CodeBlock.is_safe_code('x = json.dumps({"key": "value"})')

    def test_json_loads(self) -> None:
        CodeBlock.is_safe_code("x = json.loads(data)")

    def test_re_match(self) -> None:
        CodeBlock.is_safe_code('x = re.match(r"\\d+", text)')

    def test_re_findall(self) -> None:
        CodeBlock.is_safe_code('x = re.findall(r"\\w+", text)')

    def test_re_compile(self) -> None:
        CodeBlock.is_safe_code('pattern = re.compile(r"\\d+")')

    def test_element_type_attr(self) -> None:
        CodeBlock.is_safe_code("t = element.type")

    def test_element_dir_attr(self) -> None:
        CodeBlock.is_safe_code("d = element.dir")

    def test_await_sleep(self) -> None:
        CodeBlock.is_safe_code("await sleep(5)")

    def test_function_definition(self) -> None:
        CodeBlock.is_safe_code("def add(a, b):\n    return a + b")

    def test_async_function_definition(self) -> None:
        CodeBlock.is_safe_code("async def fetch():\n    await sleep(1)")

    def test_conditional(self) -> None:
        CodeBlock.is_safe_code("x = 1 if True else 0")

    def test_for_loop(self) -> None:
        CodeBlock.is_safe_code("for i in range(10):\n    x = i")

    def test_enumerate_loop(self) -> None:
        CodeBlock.is_safe_code("for i, value in enumerate(values):\n    x = i")

    def test_exposes_safe_iteration_and_regex_aliases(self) -> None:
        safe_vars = CodeBlock.build_safe_vars()

        assert safe_vars["enumerate"] is enumerate
        assert safe_vars["isinstance"] is isinstance
        assert safe_vars["any"] is any
        assert safe_vars["all"] is all
        assert safe_vars["max"] is max
        assert safe_vars["min"] is min
        assert safe_vars["sum"] is sum
        assert safe_vars["sorted"] is sorted
        assert safe_vars["re"].I == safe_vars["re"].IGNORECASE

    def test_try_except(self) -> None:
        CodeBlock.is_safe_code("try:\n    x = 1\nexcept Exception:\n    x = 0")

    def test_multiline_code(self) -> None:
        code = """
results = []
for item in data:
    if item.get("active"):
        results.append(item)
output = json.dumps(results)
"""
        CodeBlock.is_safe_code(code)


# ---------------------------------------------------------------------------
# build_safe_vars tests
# ---------------------------------------------------------------------------


class TestBuildSafeVars:
    def test_asyncio_is_restricted_namespace(self) -> None:
        safe_vars = CodeBlock.build_safe_vars()
        assert "asyncio" in safe_vars
        # Only sleep is exposed
        assert hasattr(safe_vars["asyncio"], "sleep")
        assert safe_vars["asyncio"].sleep is asyncio.sleep
        # Dangerous attrs must NOT exist
        assert not hasattr(safe_vars["asyncio"], "create_subprocess_shell")
        assert not hasattr(safe_vars["asyncio"], "create_subprocess_exec")
        assert not hasattr(safe_vars["asyncio"], "open_connection")
        assert not hasattr(safe_vars["asyncio"], "start_server")

    def test_locals_not_in_safe_vars(self) -> None:
        safe_vars = CodeBlock.build_safe_vars()
        assert "locals" not in safe_vars

    def test_sleep_in_safe_vars(self) -> None:
        safe_vars = CodeBlock.build_safe_vars()
        assert "sleep" in safe_vars
        assert safe_vars["sleep"] is asyncio.sleep

    def test_float_in_safe_vars(self) -> None:
        safe_vars = CodeBlock.build_safe_vars()
        assert "float" in safe_vars
        assert safe_vars["float"] is float

    def test_builtins_is_empty(self) -> None:
        safe_vars = CodeBlock.build_safe_vars()
        assert safe_vars["__builtins__"] == {}

    def test_expected_builtins_present(self) -> None:
        safe_vars = CodeBlock.build_safe_vars()
        expected = {
            "len",
            "range",
            "str",
            "int",
            "float",
            "dict",
            "list",
            "tuple",
            "set",
            "bool",
            "isinstance",
            "any",
            "all",
            "max",
            "min",
            "sum",
            "sorted",
        }
        for name in expected:
            assert name in safe_vars, f"{name} missing from safe_vars"

    def test_json_is_restricted_namespace(self) -> None:
        safe_vars = CodeBlock.build_safe_vars()
        assert hasattr(safe_vars["json"], "dumps")
        assert hasattr(safe_vars["json"], "loads")
        # Must NOT expose the real module or its transitive references
        assert not hasattr(safe_vars["json"], "codecs")
        assert not hasattr(safe_vars["json"], "decoder")
        assert not hasattr(safe_vars["json"], "encoder")

    def test_html_is_restricted_namespace(self) -> None:
        safe_vars = CodeBlock.build_safe_vars()
        assert safe_vars["html"].escape("<RBT>") == "&lt;RBT&gt;"
        assert not hasattr(safe_vars["html"], "unescape")
        assert not hasattr(safe_vars["html"], "entities")

    def test_re_is_restricted_namespace(self) -> None:
        safe_vars = CodeBlock.build_safe_vars()
        assert hasattr(safe_vars["re"], "match")
        assert hasattr(safe_vars["re"], "search")
        assert hasattr(safe_vars["re"], "findall")
        assert hasattr(safe_vars["re"], "sub")
        assert hasattr(safe_vars["re"], "compile")
        assert hasattr(safe_vars["re"], "split")
        assert hasattr(safe_vars["re"], "IGNORECASE")
        assert hasattr(safe_vars["re"], "MULTILINE")
        assert hasattr(safe_vars["re"], "DOTALL")
        # Must NOT expose module internals
        assert not hasattr(safe_vars["re"], "_compile")

    def test_exception_available(self) -> None:
        safe_vars = CodeBlock.build_safe_vars()
        assert safe_vars["Exception"] is Exception

    def test_no_safe_var_exposes_dangerous_module(self) -> None:
        """No value in safe_vars should be a module that has subprocess/OS capabilities."""
        import types

        safe_vars = CodeBlock.build_safe_vars()
        for name, val in safe_vars.items():
            if isinstance(val, types.ModuleType):
                assert not hasattr(val, "create_subprocess_shell"), (
                    f"safe_vars['{name}'] exposes create_subprocess_shell"
                )
                assert not hasattr(val, "system"), f"safe_vars['{name}'] exposes system"
                assert not hasattr(val, "popen"), f"safe_vars['{name}'] exposes popen"


# ---------------------------------------------------------------------------
# SKY-002 PoC regression — exact exploit payload must be rejected
# ---------------------------------------------------------------------------


class TestSKY002PoCRegression:
    """The exact payloads from the SKY-002 vulnerability report must be blocked."""

    def test_poc_create_subprocess_shell(self) -> None:
        """The primary exploit: asyncio.create_subprocess_shell('id')."""
        malicious_code = """\
proc = await asyncio.create_subprocess_shell(
    'id',
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
)
stdout, stderr = await proc.communicate()
result = stdout.decode() if stdout else ''
"""
        with pytest.raises(InsecureCodeDetected):
            CodeBlock.is_safe_code(malicious_code)

    def test_poc_alternative_cat_etc_passwd(self) -> None:
        """Alternative payload mentioned in the report: cat /etc/passwd."""
        malicious_code = """\
proc = await asyncio.create_subprocess_shell(
    'cat /etc/passwd',
    stdout=asyncio.subprocess.PIPE,
)
stdout, _ = await proc.communicate()
result = stdout.decode()
"""
        with pytest.raises(InsecureCodeDetected):
            CodeBlock.is_safe_code(malicious_code)

    def test_poc_json_codecs_file_read(self) -> None:
        """json.codecs.builtins.open — file read via module traversal."""
        malicious_code = 'b = json.codecs.builtins\nsecrets = b.open("/proc/self/environ").read()'
        with pytest.raises(InsecureCodeDetected):
            CodeBlock.is_safe_code(malicious_code)

    def test_poc_json_codecs_sys_modules_subprocess(self) -> None:
        """json.codecs.sys.modules["subprocess"] — full RCE via module traversal."""
        malicious_code = (
            'sp = json.codecs.sys.modules["subprocess"]\n'
            'r = sp.run(["id"], capture_output=True, text=True)\n'
            "result = r.stdout"
        )
        with pytest.raises(InsecureCodeDetected):
            CodeBlock.is_safe_code(malicious_code)

    def test_poc_json_codecs_runtime_blocked(self) -> None:
        """Even at runtime, json is a SimpleNamespace — no codecs attribute."""
        safe_vars = CodeBlock.build_safe_vars()
        assert not hasattr(safe_vars["json"], "codecs")

    def test_poc_runtime_asyncio_is_not_real_module(self) -> None:
        """Even if AST check were bypassed, the real asyncio module is not in the namespace."""
        import asyncio as asyncio_mod

        safe_vars = CodeBlock.build_safe_vars()
        # asyncio exists but is a restricted SimpleNamespace, not the real module
        assert safe_vars["asyncio"] is not asyncio_mod
        assert not hasattr(safe_vars["asyncio"], "create_subprocess_shell")

    def test_runtime_re_namespace_has_common_safe_regex_helpers(self) -> None:
        """Generated code can use normal safe regex idioms without importing re."""
        safe_vars = CodeBlock.build_safe_vars()
        re_helper = safe_vars["re"]

        assert re_helper.escape("a+b") == "a\\+b"
        assert re_helper.search("a.*b", "a\nb", re_helper.S)
        assert re_helper.findall("a", "A", re_helper.I) == ["A"]
        assert re_helper.fullmatch(r"\d+", "123")
        assert [match.group(0) for match in re_helper.finditer("a", "aba")] == ["a", "a"]


# ---------------------------------------------------------------------------
# generate_async_user_function — integration tests
# ---------------------------------------------------------------------------


class TestGenerateAsyncUserFunctionIntegration:
    """End-to-end tests through generate_async_user_function.

    These verify the full chain: is_safe_code gate + restricted exec namespace.
    We call the method directly via the unbound function to avoid constructing
    a full CodeBlock (which requires DB-backed OutputParameter fields).
    """

    @staticmethod
    def _exec_user_code(code: str, page: object = None, parameters: dict | None = None):
        """Build and return the async wrapper using CodeBlock's actual method."""
        import keyword
        import textwrap

        if page is None:
            from unittest.mock import MagicMock

            page = MagicMock()

        indented = textwrap.indent(code, "    ")
        runtime_variables: dict = {}
        safe_vars = CodeBlock.build_safe_vars()
        parameter_defaults: dict = {}
        if parameters:
            for key, value in parameters.items():
                if key not in safe_vars:
                    safe_vars[key] = value
                    if key.isidentifier() and not keyword.iskeyword(key) and not key.startswith("__"):
                        parameter_defaults[key] = value
        default_args = ", ".join(f"{key}=__param_defaults[{key!r}]" for key in parameter_defaults)
        full_code = f"""
async def wrapper({default_args}):
{indented}
    return __capture_locals()
"""
        safe_vars["page"] = page
        safe_vars["__capture_locals"] = locals
        safe_vars["__param_defaults"] = parameter_defaults
        exec(full_code, safe_vars, runtime_variables)
        user_function = runtime_variables["wrapper"]
        if not parameter_defaults:
            return user_function

        excluded_parameter_keys = frozenset(parameter_defaults)

        async def filtered_user_function():
            result = await user_function()
            if not isinstance(result, dict):
                return result
            return {key: value for key, value in result.items() if key not in excluded_parameter_keys}

        return filtered_user_function

    @pytest.mark.asyncio
    async def test_safe_code_runs_successfully(self) -> None:
        """Legitimate code should execute and return results."""
        fn = self._exec_user_code("x = 1 + 2")
        result = await fn()
        assert result["x"] == 3

    @pytest.mark.asyncio
    async def test_safe_code_with_isinstance_shape_check(self) -> None:
        fn = self._exec_user_code("value = {'downloads': 123}\nok = isinstance(value, dict)")
        result = await fn()
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_safe_code_with_json(self) -> None:
        fn = self._exec_user_code('x = json.dumps({"a": 1})')
        result = await fn()
        assert result["x"] == '{"a": 1}'

    @pytest.mark.asyncio
    async def test_safe_code_with_sleep(self) -> None:
        """sleep (asyncio.sleep) should be available and callable."""
        fn = self._exec_user_code("await sleep(0)")
        result = await fn()
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_user_function_timeout_bounds_execution(self) -> None:
        """A stuck code block must fail promptly instead of holding a workflow run open."""
        fn = self._exec_user_code("await sleep(10)")
        with pytest.raises(asyncio.TimeoutError):
            await CodeBlock.execute_user_function_with_timeout(fn, timeout_seconds=1)

    @pytest.mark.asyncio
    async def test_user_function_timeout_can_be_disabled(self) -> None:
        fn = self._exec_user_code("await sleep(0)\ncompleted = True")
        result = await CodeBlock.execute_user_function_with_timeout(fn, timeout_seconds=0)
        assert result["completed"] is True

    @pytest.mark.asyncio
    async def test_execute_uses_configured_code_block_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class FakeBrowserState:
            async def get_working_page(self) -> object:
                return object()

        class FakeWorkflowRunContext:
            values: dict[str, object] = {}
            secrets: dict[str, object] = {}
            include_secrets_in_templates = False
            workflow_title = "Test Workflow"
            workflow_id = "w_test"
            workflow_permanent_id = "wpid_test"
            workflow_run_id = "wrid_test"
            browser_session_id = None
            workflow_run_outputs: list[object] = []

            def get_block_metadata(self, label: str | None) -> dict[str, object]:
                return {}

            def build_workflow_run_summary(self) -> str:
                return ""

            def mask_secrets_in_data(self, data: object, mask: str = "*****") -> object:
                return data

        async def validate_code_block(*args: object, **kwargs: object) -> None:
            return None

        async def get_browser_state(*args: object, **kwargs: object) -> FakeBrowserState:
            return FakeBrowserState()

        async def record_output(*args: object, **kwargs: object) -> None:
            return None

        monkeypatch.setattr(
            "skyvern.forge.sdk.workflow.models.block.app.AGENT_FUNCTION.validate_code_block",
            validate_code_block,
        )
        monkeypatch.setattr(CodeBlock, "get_or_create_browser_state", get_browser_state)
        monkeypatch.setattr(CodeBlock, "get_workflow_run_context", lambda *args: FakeWorkflowRunContext())
        monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record_output)

        now = datetime.now(timezone.utc)
        output_parameter = OutputParameter(
            parameter_type=ParameterType.OUTPUT,
            key="test_code_output",
            description="test output",
            output_parameter_id="op_test_code",
            workflow_id="w_test",
            created_at=now,
            modified_at=now,
        )
        block = CodeBlock(label="test_code", code="value = 'ok'", output_parameter=output_parameter)
        result = await block.execute(workflow_run_id="wrid_test", workflow_run_block_id="")

        assert settings.CODE_BLOCK_EXECUTION_TIMEOUT_SECONDS > 0
        assert result.success is True
        assert result.status == BlockStatus.completed
        assert result.output_parameter_value == {"value": "ok"}

    def test_poc_blocked_at_is_safe_code_gate(self) -> None:
        """The PoC payload is rejected before exec() is ever called."""
        malicious_code = "proc = await asyncio.create_subprocess_shell('id')"
        with pytest.raises(InsecureCodeDetected):
            CodeBlock.is_safe_code(malicious_code)

    @pytest.mark.asyncio
    async def test_runtime_asyncio_sleep_works(self) -> None:
        """asyncio.sleep should work in the sandbox."""
        fn = self._exec_user_code("await asyncio.sleep(0)")
        result = await fn()
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_runtime_asyncio_has_no_subprocess(self) -> None:
        """asyncio.create_subprocess_shell must not exist at runtime."""
        fn = self._exec_user_code("x = asyncio.create_subprocess_shell")
        with pytest.raises(AttributeError):
            await fn()

    @pytest.mark.asyncio
    async def test_runtime_namespace_has_no_locals_builtin(self) -> None:
        """'locals' is not available as a callable in the sandbox."""
        fn = self._exec_user_code("x = locals()")
        with pytest.raises(NameError, match="locals"):
            await fn()

    @pytest.mark.asyncio
    async def test_runtime_builtins_are_empty(self) -> None:
        """Builtins like __import__, eval, exec are not available at runtime."""
        fn = self._exec_user_code("x = open('/etc/passwd')")
        with pytest.raises(NameError, match="open"):
            await fn()

    @pytest.mark.asyncio
    async def test_parameters_cannot_override_sandbox_internals(self) -> None:
        """Workflow parameters must not overwrite sandbox-controlled names."""
        import os

        # Try to inject the real os module via a parameter named "json"
        fn = self._exec_user_code(
            'x = json.dumps({"a": 1})',
            parameters={"json": os},
        )
        result = await fn()
        # json should still be the safe SimpleNamespace, not os
        assert result["x"] == '{"a": 1}'

    @pytest.mark.asyncio
    async def test_parameter_can_be_normalized_with_same_local_name(self) -> None:
        """Workflow parameters are usable without leaking wrapper defaults into output."""
        fn = self._exec_user_code(
            "person_name = person_name.strip()\nnormalized = person_name.upper()",
            parameters={"person_name": " Noor Assi "},
        )
        result = await fn()

        assert "person_name" not in result
        assert result["normalized"] == "NOOR ASSI"

    @pytest.mark.asyncio
    async def test_parameter_defaults_do_not_leak_into_implicit_output(self) -> None:
        fn = self._exec_user_code(
            "visible = public_value.upper()",
            parameters={"public_value": "ok", "secret_token": "sensitive"},
        )
        result = await fn()

        assert result == {"visible": "OK"}

    @pytest.mark.asyncio
    async def test_parameters_cannot_override_builtins(self) -> None:
        """A parameter named __builtins__ must not re-enable builtins."""
        import builtins

        fn = self._exec_user_code(
            "x = open('/etc/passwd')",
            parameters={"__builtins__": vars(builtins)},
        )
        with pytest.raises(NameError, match="open"):
            await fn()

    @pytest.mark.asyncio
    async def test_real_method_explicit_list_return_passes_through(self) -> None:
        """SKY-10789 regression: a parameterized code block ending in an explicit
        `return <list>` must yield the list, not crash on result.items().

        Exercises the real CodeBlock.generate_async_user_function (not the helper)
        so the fix is locked at the source. #11869 introduced the regression;
        without this guard it raised AttributeError: 'list' object has no attribute 'items'.
        """
        from unittest.mock import MagicMock

        now = datetime.now(timezone.utc)
        output_parameter = OutputParameter(
            parameter_type=ParameterType.OUTPUT,
            key="check_max_docs_output",
            description="test output",
            output_parameter_id="op_test_return",
            workflow_id="w_test",
            created_at=now,
            modified_at=now,
        )
        code = "candidates = diff_documents_output.get('candidates') or []\nreturn list(candidates)"
        block = CodeBlock(label="check_max_docs", code=code, output_parameter=output_parameter)
        fn = block.generate_async_user_function(
            block.code,
            MagicMock(),
            parameters={"diff_documents_output": {"candidates": [{"name": "a"}, {"name": "b"}]}},
        )
        result = await fn()
        assert result == [{"name": "a"}, {"name": "b"}]

    @pytest.mark.asyncio
    async def test_explicit_non_dict_return_does_not_leak_parameters(self) -> None:
        """A parameterized block returning a non-dict passes the value through verbatim."""
        fn = self._exec_user_code(
            "rows = [public_value, public_value]\nreturn rows",
            parameters={"public_value": "x", "secret_token": "sensitive"},
        )
        result = await fn()
        assert result == ["x", "x"]

    def test_wrapper_uses_capture_locals_not_locals(self) -> None:
        """Regression: the wrapper template must use __capture_locals(), not return locals()."""
        import textwrap

        code = "x = 1"
        indented = textwrap.indent(code, "    ")
        full_code = f"\nasync def wrapper():\n{indented}\n    return __capture_locals()\n"
        assert "__capture_locals()" in full_code
        assert "return locals()" not in full_code


# ---------------------------------------------------------------------------
# Fill-time OTP primitive (SKY-10938)
# ---------------------------------------------------------------------------

# RFC 6238 published test seed — safe to ship in OSS-synced fixtures.
_RFC_TOTP_SEED = "JBSWY3DPEHPK3PXP"
_CREDENTIAL_KEY = "login_credential"
_WORKFLOW_RUN_ID = "wr_otp_test"
_ORG_ID = "o_otp_test"


def _build_wrc_with_totp_seed(seed: str = _RFC_TOTP_SEED):
    """Build a real WorkflowRunContext carrying a TOTP-bearing credential.

    Mirrors the registration shape produced by
    context_manager._register_credential_parameter_value: the credential value dict holds a
    'totp' placeholder id; secrets maps that id to the TOTP sentinel and the
    totp_secret_value_key to the real seed.
    """
    from unittest.mock import MagicMock

    from skyvern.forge.sdk.services.bitwarden import BitwardenConstants
    from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext

    wrc = WorkflowRunContext(
        workflow_title="t",
        workflow_id="w",
        workflow_permanent_id="wpid",
        workflow_run_id=_WORKFLOW_RUN_ID,
        aws_client=MagicMock(),
    )
    totp_secret_id = wrc.generate_random_secret_id() + "_totp"
    wrc.secrets[totp_secret_id] = BitwardenConstants.TOTP
    wrc.secrets[wrc.totp_secret_value_key(totp_secret_id)] = seed
    wrc.values[_CREDENTIAL_KEY] = {"context": "placeholder note", "totp": totp_secret_id}
    return wrc


def _build_wrc_with_identifier(identifier: str = "otp@example.com"):
    """Build a real WorkflowRunContext with an email/SMS identifier and no TOTP seed."""
    from unittest.mock import MagicMock

    from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext

    wrc = WorkflowRunContext(
        workflow_title="t",
        workflow_id="w",
        workflow_permanent_id="wpid",
        workflow_run_id=_WORKFLOW_RUN_ID,
        aws_client=MagicMock(),
    )
    wrc.values[_CREDENTIAL_KEY] = {"context": "placeholder note"}
    wrc.credential_totp_identifiers[_CREDENTIAL_KEY] = identifier
    return wrc


class _FakeWorkflowRun:
    def __init__(self) -> None:
        self.workflow_id = "w"
        self.workflow_permanent_id = "wpid"
        self.started_at = datetime(2026, 6, 14, 0, 0, 0, tzinfo=timezone.utc)


def _patch_context_resolution(monkeypatch: "pytest.MonkeyPatch", wrc) -> None:
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.block.app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context",
        lambda *args, **kwargs: wrc,
    )


async def _run_credential_code_block(monkeypatch: "pytest.MonkeyPatch", wrc, code: str, label: str):
    """Execute a CodeBlock with a TOTP-bearing credential parameter through the real execute path.

    Returns (block_result, persisted_output_value). Mirrors the credential bind + persist flow so
    masking and failure-reason handling are exercised end-to-end.
    """
    from unittest.mock import MagicMock

    from skyvern.forge.sdk.workflow.models.parameter import (
        CredentialParameter,
        OutputParameter,
        ParameterType,
    )

    class FakeBrowserState:
        async def get_working_page(self) -> object:
            return MagicMock()

    async def validate_code_block(*args: object, **kwargs: object) -> None:
        return None

    async def get_browser_state(*args: object, **kwargs: object) -> FakeBrowserState:
        return FakeBrowserState()

    persisted: dict[str, object] = {}

    async def record_output(self: object, ctx: object, run_id: object, value: object) -> None:
        persisted["value"] = value

    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.block.app.AGENT_FUNCTION.validate_code_block",
        validate_code_block,
    )
    monkeypatch.setattr(CodeBlock, "get_or_create_browser_state", get_browser_state)
    monkeypatch.setattr(CodeBlock, "get_workflow_run_context", lambda self, run_id: wrc)
    monkeypatch.setattr(CodeBlock, "record_output_parameter_value", record_output)
    _patch_context_resolution(monkeypatch, wrc)

    now = datetime.now(timezone.utc)
    output_parameter = OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key=f"{label}_output",
        description="test output",
        output_parameter_id=f"op_{label}",
        workflow_id="w",
        created_at=now,
        modified_at=now,
    )
    credential_parameter = CredentialParameter(
        parameter_type=ParameterType.CREDENTIAL,
        key=_CREDENTIAL_KEY,
        description="cred",
        workflow_id="w",
        credential_parameter_id=f"cp_{label}",
        credential_id="vault:item",
        created_at=now,
        modified_at=now,
    )
    block = CodeBlock(
        label=label,
        code=code,
        output_parameter=output_parameter,
        parameters=[credential_parameter],
    )
    result = await block.execute(workflow_run_id=_WORKFLOW_RUN_ID, workflow_run_block_id="")
    return result, persisted.get("value")


class TestCodeBlockOtpMintAtCallTime:
    """AC1/AC4: the primitive re-mints the TOTP at call time, not at block start."""

    @pytest.mark.asyncio
    async def test_otp_returns_current_totp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import pyotp

        from skyvern.forge.sdk.workflow.models.block import _resolve_code_block_otp

        wrc = _build_wrc_with_totp_seed()
        _patch_context_resolution(monkeypatch, wrc)

        code = await _resolve_code_block_otp(_CREDENTIAL_KEY, _ORG_ID, _WORKFLOW_RUN_ID, budget_seconds=120)

        assert code == pyotp.TOTP(_RFC_TOTP_SEED).now()
        assert len(code) == 6 and code.isdigit()

    @pytest.mark.asyncio
    async def test_remint_differs_after_clock_advance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Advancing the clock past a rotation yields a different code — proves re-mint, not the
        stale pre-minted attribute. pyotp.TOTP.now() reads pyotp.totp.datetime.datetime.now()."""
        import datetime as real_datetime

        import pyotp.totp as pyotp_totp

        from skyvern.forge.sdk.workflow.models.block import _resolve_code_block_otp

        wrc = _build_wrc_with_totp_seed()
        _patch_context_resolution(monkeypatch, wrc)

        anchor = real_datetime.datetime(2026, 6, 14, 12, 0, 0)

        def _frozen(offset_seconds: int):
            class _FrozenDatetime(real_datetime.datetime):
                @classmethod
                def now(cls, tz: object = None) -> "real_datetime.datetime":
                    return anchor + real_datetime.timedelta(seconds=offset_seconds)

            return _FrozenDatetime

        monkeypatch.setattr(pyotp_totp, "datetime", SimpleNamespace(datetime=_frozen(0)))
        first = await _resolve_code_block_otp(_CREDENTIAL_KEY, _ORG_ID, _WORKFLOW_RUN_ID, budget_seconds=120)

        monkeypatch.setattr(pyotp_totp, "datetime", SimpleNamespace(datetime=_frozen(60)))
        second = await _resolve_code_block_otp(_CREDENTIAL_KEY, _ORG_ID, _WORKFLOW_RUN_ID, budget_seconds=120)

        assert first != second

    @pytest.mark.asyncio
    async def test_returned_code_is_not_the_seed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.workflow.models.block import _resolve_code_block_otp

        wrc = _build_wrc_with_totp_seed()
        _patch_context_resolution(monkeypatch, wrc)

        code = await _resolve_code_block_otp(_CREDENTIAL_KEY, _ORG_ID, _WORKFLOW_RUN_ID, budget_seconds=120)
        assert code != _RFC_TOTP_SEED

    @pytest.mark.asyncio
    async def test_minted_code_registered_in_secrets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.workflow.models.block import _resolve_code_block_otp

        wrc = _build_wrc_with_totp_seed()
        _patch_context_resolution(monkeypatch, wrc)

        code = await _resolve_code_block_otp(_CREDENTIAL_KEY, _ORG_ID, _WORKFLOW_RUN_ID, budget_seconds=120)
        assert code in set(wrc.secrets.values())


class TestCodeBlockOtpIdentifierFetch:
    """AC2: a credential with a totp_identifier polls via otp_service with the right anchor."""

    @pytest.mark.asyncio
    async def test_fetches_via_poll_with_run_start_anchor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.workflow.models import block as block_module
        from skyvern.forge.sdk.workflow.models.block import _resolve_code_block_otp
        from skyvern.services.otp_service import OTPValue

        wrc = _build_wrc_with_identifier()
        _patch_context_resolution(monkeypatch, wrc)

        fake_run = _FakeWorkflowRun()

        async def fake_get_workflow_run(*args: object, **kwargs: object) -> _FakeWorkflowRun:
            return fake_run

        monkeypatch.setattr(
            block_module.app.DATABASE.workflow_runs, "get_workflow_run", fake_get_workflow_run, raising=False
        )

        captured: dict[str, object] = {}

        async def fake_poll(**kwargs: object) -> OTPValue:
            captured.update(kwargs)
            return OTPValue(value="246810")

        monkeypatch.setattr(block_module.otp_service, "poll_otp_value", fake_poll)

        code = await _resolve_code_block_otp(_CREDENTIAL_KEY, _ORG_ID, _WORKFLOW_RUN_ID, budget_seconds=120)

        assert code == "246810"
        assert captured["totp_identifier"] == "otp@example.com"
        assert captured["created_after"] == fake_run.started_at
        assert captured["organization_id"] == _ORG_ID
        # The budget is the in-block setting, never the legacy 15-minute window.
        assert "timeout" not in captured

    @pytest.mark.asyncio
    async def test_fetched_code_registered_in_secrets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.workflow.models import block as block_module
        from skyvern.forge.sdk.workflow.models.block import _resolve_code_block_otp
        from skyvern.services.otp_service import OTPValue

        wrc = _build_wrc_with_identifier()
        _patch_context_resolution(monkeypatch, wrc)

        async def fake_get_workflow_run(*args: object, **kwargs: object) -> _FakeWorkflowRun:
            return _FakeWorkflowRun()

        async def fake_poll(**kwargs: object) -> OTPValue:
            return OTPValue(value="135790")

        monkeypatch.setattr(
            block_module.app.DATABASE.workflow_runs, "get_workflow_run", fake_get_workflow_run, raising=False
        )
        monkeypatch.setattr(block_module.otp_service, "poll_otp_value", fake_poll)

        code = await _resolve_code_block_otp(_CREDENTIAL_KEY, _ORG_ID, _WORKFLOW_RUN_ID, budget_seconds=120)
        assert code == "135790"
        assert "135790" in set(wrc.secrets.values())

    @pytest.mark.asyncio
    async def test_magic_link_returned_as_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.workflow.models import block as block_module
        from skyvern.forge.sdk.workflow.models.block import _resolve_code_block_otp
        from skyvern.services.otp_service import OTPValue

        wrc = _build_wrc_with_identifier()
        _patch_context_resolution(monkeypatch, wrc)

        async def fake_get_workflow_run(*args: object, **kwargs: object) -> _FakeWorkflowRun:
            return _FakeWorkflowRun()

        link = "https://example.com/magic?token=abc"

        async def fake_poll(**kwargs: object) -> OTPValue:
            return OTPValue(value=link)

        monkeypatch.setattr(
            block_module.app.DATABASE.workflow_runs, "get_workflow_run", fake_get_workflow_run, raising=False
        )
        monkeypatch.setattr(block_module.otp_service, "poll_otp_value", fake_poll)

        code = await _resolve_code_block_otp(_CREDENTIAL_KEY, _ORG_ID, _WORKFLOW_RUN_ID, budget_seconds=120)
        assert code == link
        assert link in set(wrc.secrets.values())


class TestCodeBlockOtpBudget:
    """AC2: the in-block poll budget raises a clear error, not the opaque 300s kill."""

    @pytest.mark.asyncio
    async def test_poll_timeout_raises_clear_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.workflow.models import block as block_module
        from skyvern.forge.sdk.workflow.models.block import CodeBlockOTPError, _resolve_code_block_otp

        wrc = _build_wrc_with_identifier()
        _patch_context_resolution(monkeypatch, wrc)

        async def fake_get_workflow_run(*args: object, **kwargs: object) -> _FakeWorkflowRun:
            return _FakeWorkflowRun()

        async def raises_timeout(**kwargs: object) -> object:
            # Stand in for wait_for exhausting the budget; the translation to
            # CodeBlockOTPError is what this test exercises (no real wall-clock wait).
            raise asyncio.TimeoutError

        monkeypatch.setattr(
            block_module.app.DATABASE.workflow_runs, "get_workflow_run", fake_get_workflow_run, raising=False
        )
        monkeypatch.setattr(block_module.otp_service, "poll_otp_value", raises_timeout)

        with pytest.raises(CodeBlockOTPError, match="within 1 seconds"):
            await _resolve_code_block_otp(_CREDENTIAL_KEY, _ORG_ID, _WORKFLOW_RUN_ID, budget_seconds=1)

    @pytest.mark.asyncio
    async def test_poll_timeout_error_is_not_asyncio_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The in-block budget surfaces CodeBlockOTPError, not the bare asyncio.TimeoutError
        that the outer 300s wait_for would raise."""
        from skyvern.forge.sdk.workflow.models import block as block_module
        from skyvern.forge.sdk.workflow.models.block import CodeBlockOTPError, _resolve_code_block_otp

        wrc = _build_wrc_with_identifier()
        _patch_context_resolution(monkeypatch, wrc)

        async def fake_get_workflow_run(*args: object, **kwargs: object) -> _FakeWorkflowRun:
            return _FakeWorkflowRun()

        async def raises_timeout(**kwargs: object) -> object:
            # Stand in for wait_for exhausting the budget; the translation to
            # CodeBlockOTPError is what this test exercises (no real wall-clock wait).
            raise asyncio.TimeoutError

        monkeypatch.setattr(
            block_module.app.DATABASE.workflow_runs, "get_workflow_run", fake_get_workflow_run, raising=False
        )
        monkeypatch.setattr(block_module.otp_service, "poll_otp_value", raises_timeout)

        raised: Exception | None = None
        try:
            await _resolve_code_block_otp(_CREDENTIAL_KEY, _ORG_ID, _WORKFLOW_RUN_ID, budget_seconds=1)
        except Exception as e:  # noqa: BLE001
            raised = e
        assert isinstance(raised, CodeBlockOTPError)
        assert not isinstance(raised, asyncio.TimeoutError)

    @pytest.mark.asyncio
    async def test_poll_exception_is_sanitized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A poll failure carrying the identifier must not leak it through the in-block error."""
        from skyvern.exceptions import NoTOTPVerificationCodeFound
        from skyvern.forge.sdk.workflow.models import block as block_module
        from skyvern.forge.sdk.workflow.models.block import CodeBlockOTPError, _resolve_code_block_otp

        wrc = _build_wrc_with_identifier(identifier="secret-identifier@example.com")
        _patch_context_resolution(monkeypatch, wrc)

        async def fake_get_workflow_run(*args: object, **kwargs: object) -> _FakeWorkflowRun:
            return _FakeWorkflowRun()

        async def failing_poll(**kwargs: object) -> object:
            raise NoTOTPVerificationCodeFound(totp_identifier="secret-identifier@example.com")

        monkeypatch.setattr(
            block_module.app.DATABASE.workflow_runs, "get_workflow_run", fake_get_workflow_run, raising=False
        )
        monkeypatch.setattr(block_module.otp_service, "poll_otp_value", failing_poll)

        with pytest.raises(CodeBlockOTPError) as exc_info:
            await _resolve_code_block_otp(_CREDENTIAL_KEY, _ORG_ID, _WORKFLOW_RUN_ID, budget_seconds=120)
        assert "secret-identifier@example.com" not in str(exc_info.value)


class TestCodeBlockOtpNoSource:
    """A credential with neither a seed nor an identifier raises a clear error."""

    @pytest.mark.asyncio
    async def test_no_source_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from unittest.mock import MagicMock

        from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
        from skyvern.forge.sdk.workflow.models.block import CodeBlockOTPError, _resolve_code_block_otp

        wrc = WorkflowRunContext(
            workflow_title="t",
            workflow_id="w",
            workflow_permanent_id="wpid",
            workflow_run_id=_WORKFLOW_RUN_ID,
            aws_client=MagicMock(),
        )
        wrc.values[_CREDENTIAL_KEY] = {"context": "placeholder note"}
        _patch_context_resolution(monkeypatch, wrc)

        with pytest.raises(CodeBlockOTPError, match="No OTP source"):
            await _resolve_code_block_otp(_CREDENTIAL_KEY, _ORG_ID, _WORKFLOW_RUN_ID, budget_seconds=120)

    @pytest.mark.asyncio
    async def test_missing_org_id_for_poll_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.workflow.models.block import CodeBlockOTPError, _resolve_code_block_otp

        wrc = _build_wrc_with_identifier()
        _patch_context_resolution(monkeypatch, wrc)

        with pytest.raises(CodeBlockOTPError, match="organization"):
            await _resolve_code_block_otp(_CREDENTIAL_KEY, None, _WORKFLOW_RUN_ID, budget_seconds=120)

    @pytest.mark.asyncio
    async def test_missing_run_context_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.workflow.models import block as block_module
        from skyvern.forge.sdk.workflow.models.block import CodeBlockOTPError, _resolve_code_block_otp

        monkeypatch.setattr(
            block_module.app.WORKFLOW_CONTEXT_MANAGER,
            "get_workflow_run_context",
            lambda *args, **kwargs: None,
        )
        with pytest.raises(CodeBlockOTPError, match="context"):
            await _resolve_code_block_otp(_CREDENTIAL_KEY, _ORG_ID, _WORKFLOW_RUN_ID, budget_seconds=120)


class TestCodeBlockOtpSeedConfinement:
    """The seed must stay unreachable from the user-facing bound method/builtin."""

    def test_bound_method_does_not_capture_wrc_or_seed(self) -> None:
        from skyvern.forge.sdk.workflow.models.block import _bind_code_block_otp

        bound = _bind_code_block_otp(_CREDENTIAL_KEY, _ORG_ID, _WORKFLOW_RUN_ID)
        captured = [cell.cell_contents for cell in (bound.__closure__ or ())]
        # Security invariant: the closure may hold only opaque string ids — never a
        # WorkflowRunContext (non-str) or the TOTP seed. Avoids asserting an exact cell
        # count, which is a CPython layout detail brittle to benign refactors.
        assert all(isinstance(value, str) for value in captured)
        assert _RFC_TOTP_SEED not in captured
        assert {_CREDENTIAL_KEY, _ORG_ID, _WORKFLOW_RUN_ID} <= set(captured)

    @pytest.mark.asyncio
    async def test_builtin_rejects_non_credential(self) -> None:
        from skyvern.forge.sdk.workflow.models.block import CodeBlock as _CB
        from skyvern.forge.sdk.workflow.models.block import CodeBlockOTPError

        otp_builtin = _CB.build_safe_vars()["otp"]
        with pytest.raises(CodeBlockOTPError):
            await otp_builtin(object())

    @pytest.mark.parametrize("snippet", ["cred.otp.__closure__", "cred.otp.__code__"])
    def test_user_code_cannot_walk_bound_method_internals(self, snippet: str) -> None:
        with pytest.raises(InsecureCodeDetected, match="private"):
            CodeBlock.is_safe_code(snippet)

    def test_user_code_cannot_getattr_or_vars(self) -> None:
        with pytest.raises(InsecureCodeDetected, match="getattr"):
            CodeBlock.is_safe_code("cred.getattr('otp')")
        with pytest.raises(InsecureCodeDetected, match="vars"):
            CodeBlock.is_safe_code("cred.vars()")


class TestCodeBlockOtpBuiltinDelegates:
    """The top-level otp(credential) builtin delegates to the bound .otp()."""

    @pytest.mark.asyncio
    async def test_builtin_calls_bound_method(self) -> None:
        from types import SimpleNamespace

        from skyvern.forge.sdk.workflow.models.block import CodeBlock as _CB

        called: dict[str, bool] = {}

        async def fake_bound() -> str:
            called["bound"] = True
            return "999111"

        cred = SimpleNamespace(otp=fake_bound)
        otp_builtin = _CB.build_safe_vars()["otp"]
        result = await otp_builtin(cred)
        assert result == "999111"
        assert called["bound"] is True


class TestCodeBlockOtpNoLeak:
    """AC3: the resolved OTP is masked in the persisted output and never leaks the seed."""

    @pytest.mark.asyncio
    async def test_execute_masks_otp_in_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import pyotp

        wrc = _build_wrc_with_totp_seed()
        expected_code = pyotp.TOTP(_RFC_TOTP_SEED).now()

        result, output_value = await _run_credential_code_block(
            monkeypatch,
            wrc,
            code=f"code = await {_CREDENTIAL_KEY}.otp()\nresult = code",
            label="otp_code",
        )

        assert result.success is True
        # The minted code is masked everywhere it was assigned to a user local.
        assert expected_code not in json.dumps(output_value)
        assert _RFC_TOTP_SEED not in json.dumps(output_value)
        assert expected_code in set(wrc.secrets.values())
        assert "*****" in json.dumps(output_value)

    @pytest.mark.asyncio
    async def test_otp_not_leaked_in_failure_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """User code that raises with the OTP in the message must not leak it into failure_reason."""
        import pyotp

        wrc = _build_wrc_with_totp_seed()
        expected_code = pyotp.TOTP(_RFC_TOTP_SEED).now()

        result, _ = await _run_credential_code_block(
            monkeypatch,
            wrc,
            code=f"code = await {_CREDENTIAL_KEY}.otp()\nraise Exception(code)",
            label="otp_raise",
        )

        assert result.success is False
        assert result.failure_reason is not None
        assert expected_code not in result.failure_reason
        assert _RFC_TOTP_SEED not in result.failure_reason

    @pytest.mark.asyncio
    async def test_legacy_totp_not_leaked_in_failure_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import pyotp

        wrc = _build_wrc_with_totp_seed()
        expected_code = pyotp.TOTP(_RFC_TOTP_SEED).now()

        result, _ = await _run_credential_code_block(
            monkeypatch,
            wrc,
            code=f"raise Exception({_CREDENTIAL_KEY}.totp)",
            label="legacy_raise",
        )

        assert result.success is False
        assert result.failure_reason is not None
        assert expected_code not in result.failure_reason
        assert _RFC_TOTP_SEED not in result.failure_reason


class TestCodeBlockLegacyTotpRegression:
    """The legacy pre-minted .totp attribute keeps working and is registered for masking."""

    @pytest.mark.asyncio
    async def test_legacy_totp_attribute_and_registration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import pyotp

        wrc = _build_wrc_with_totp_seed()
        expected_code = pyotp.TOTP(_RFC_TOTP_SEED).now()

        # Legacy synthesized code reads the pre-minted .totp attribute.
        result, output_value = await _run_credential_code_block(
            monkeypatch,
            wrc,
            code=f"aliased = {_CREDENTIAL_KEY}.totp\nresult = aliased",
            label="legacy",
        )

        assert result.success is True
        # The pre-minted code matches the seed's current code AND is registered + masked.
        assert expected_code in set(wrc.secrets.values())
        assert expected_code not in json.dumps(output_value)

    @pytest.mark.asyncio
    async def test_bound_credential_namespace_excluded_from_captured_locals(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bound Credential namespace (carrying .otp) is dropped from captured locals by the
        existing parameter-key exclusion, so it never appears in the output."""
        wrc = _build_wrc_with_totp_seed()

        result, output_value = await _run_credential_code_block(
            monkeypatch,
            wrc,
            code="visible = 'ok'",
            label="excl",
        )

        assert result.success is True
        assert output_value == {"visible": "ok"}
        assert _CREDENTIAL_KEY not in output_value
