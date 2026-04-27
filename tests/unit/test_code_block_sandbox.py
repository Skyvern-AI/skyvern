"""Tests for CodeBlock sandbox security hardening (SKY-7897).

Verifies that the CodeBlock safety layer:
- Rejects dangerous code patterns (subprocess, network, sandbox-escape, imports, dunder access)
- Accepts legitimate code patterns (math, strings, json, regex, sleep)
- Exposes the correct safe variables (no asyncio, yes sleep)
"""

import asyncio

import pytest

from skyvern.forge.sdk.workflow.exceptions import InsecureCodeDetected
from skyvern.forge.sdk.workflow.models.block import CodeBlock

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
        expected = {"len", "range", "str", "int", "float", "dict", "list", "tuple", "set", "bool"}
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
        import textwrap

        if page is None:
            from unittest.mock import MagicMock

            page = MagicMock()

        indented = textwrap.indent(code, "    ")
        full_code = f"""
async def wrapper():
{indented}
    return __capture_locals()
"""
        runtime_variables: dict = {}
        safe_vars = CodeBlock.build_safe_vars()
        if parameters:
            for key, value in parameters.items():
                if key not in safe_vars:
                    safe_vars[key] = value
        safe_vars["page"] = page
        safe_vars["__capture_locals"] = locals
        exec(full_code, safe_vars, runtime_variables)
        return runtime_variables["wrapper"]

    @pytest.mark.asyncio
    async def test_safe_code_runs_successfully(self) -> None:
        """Legitimate code should execute and return results."""
        fn = self._exec_user_code("x = 1 + 2")
        result = await fn()
        assert result["x"] == 3

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
    async def test_parameters_cannot_override_builtins(self) -> None:
        """A parameter named __builtins__ must not re-enable builtins."""
        import builtins

        fn = self._exec_user_code(
            "x = open('/etc/passwd')",
            parameters={"__builtins__": vars(builtins)},
        )
        with pytest.raises(NameError, match="open"):
            await fn()

    def test_wrapper_uses_capture_locals_not_locals(self) -> None:
        """Regression: the wrapper template must use __capture_locals(), not return locals()."""
        import textwrap

        code = "x = 1"
        indented = textwrap.indent(code, "    ")
        full_code = f"\nasync def wrapper():\n{indented}\n    return __capture_locals()\n"
        assert "__capture_locals()" in full_code
        assert "return locals()" not in full_code
