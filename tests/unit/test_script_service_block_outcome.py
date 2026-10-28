"""Outcome records from cached blocks and loops."""

import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import Error as PlaywrightError

from skyvern.core.script_generations.real_skyvern_page_ai import RealSkyvernPageAi
from skyvern.core.script_generations.script_skyvern_page import ScriptRunContextManager, ScriptSkyvernPage
from skyvern.core.script_generations.skyvern_page import RunContext
from skyvern.exceptions import BROWSER_CONNECTION_LOST_ERROR_CODE, MissingBrowserState, ScriptTerminationException
from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.context_manager import BlockOutcome, WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import (
    ActionBlock,
    Block,
    ExtractionBlock,
    FileDownloadBlock,
    ForLoopBlock,
    HttpRequestBlock,
    LoginBlock,
    NavigationBlock,
    TextPromptBlock,
    UrlBlock,
    ValidationBlock,
    WhileLoopBlock,
)
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.schemas.workflows import BlockResult, BlockStatus, BlockType
from skyvern.services import script_service
from skyvern.webeye.utils.challenge_solve_record import ChallengeSolveReceipt
from tests.unit._task_block_fakes import run_bound_browser_state
from tests.unit.conftest import make_block_output_parameter
from tests.unit.helpers import make_organization, make_task
from tests.unit.scoped_asyncio import ScopedAsyncio


class _BlockRows:
    """The workflow_run_block rows a cached script run writes, by id."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def create(self, label: str | None) -> str:
        workflow_run_block_id = f"wrb_{len(self.rows)}"
        self.rows[workflow_run_block_id] = {
            "label": label,
            "status": BlockStatus.running,
            "error_codes": None,
            "output": None,
        }
        return workflow_run_block_id

    async def update(self, workflow_run_block_id: str, **fields: Any) -> SimpleNamespace:
        row = self.rows[workflow_run_block_id]
        for key in ("status", "output"):
            if fields.get(key):
                row[key] = fields[key]
        if fields.get("error_codes") is not None:
            row["error_codes"] = fields["error_codes"]
        return SimpleNamespace(label=row["label"], error_codes=row["error_codes"])

    def labelled(self, label: str) -> list[dict[str, Any]]:
        return [row for row in self.rows.values() if row["label"] == label]


@pytest.fixture
def cached_run(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """A cached script run over the real helpers, with only the repository and storage edges faked."""
    now = datetime.now(timezone.utc)
    organization = make_organization(now)
    rows = _BlockRows()
    browser_state, browser_context = run_bound_browser_state("wr_cached")
    workflow_context = WorkflowRunContext(
        workflow_title="test",
        workflow_id="wf",
        workflow_permanent_id="wpid",
        workflow_run_id="wr_cached",
        aws_client=AsyncMock(),
    )
    run_context = skyvern_context.SkyvernContext(
        organization_id="o_cached", workflow_id="wf", workflow_run_id="wr_cached", script_mode=True
    )
    manager = ScriptRunContextManager()
    manager.set_run_context(RunContext(parameters={}, page=MagicMock()))
    workflow = SimpleNamespace(ai_fallback=False, workflow_definition=SimpleNamespace(blocks=[]))
    billed = AsyncMock()

    async def create_block(
        block_type: BlockType, label: str | None = None, **_: object
    ) -> tuple[str, str | None, str | None]:
        workflow_run_block_id = rows.create(label)
        if block_type in (BlockType.FOR_LOOP, BlockType.WAIT):
            return workflow_run_block_id, None, None
        run_context.task_id = f"tsk_{workflow_run_block_id}"
        run_context.step_id = f"stp_{workflow_run_block_id}"
        return workflow_run_block_id, run_context.task_id, run_context.step_id

    async def update_task(task_id: str, status: TaskStatus, failure_reason: str | None = None, **_: object) -> Any:
        return make_task(now, organization, task_id=task_id, status=status, failure_reason=failure_reason)

    monkeypatch.setattr(script_service, "script_run_context_manager", manager)
    monkeypatch.setattr(script_service, "_create_workflow_block_run_and_task", create_block)
    monkeypatch.setattr(
        script_service,
        "_validate_and_get_output_parameter",
        AsyncMock(
            return_value=script_service.BlockValidationOutput(
                context=run_context,
                label="rows",
                output_parameter=make_block_output_parameter("rows_output"),
                input_parameters=[],
                workflow=workflow,
                workflow_id="wf",
                workflow_run_id="wr_cached",
                organization_id="o_cached",
            )
        ),
    )
    monkeypatch.setattr(script_service, "run_lease_lost", AsyncMock(return_value=False))
    monkeypatch.setattr(script_service, "_record_output_parameter_value", AsyncMock())
    monkeypatch.setattr(
        app.WORKFLOW_CONTEXT_MANAGER, "get_workflow_run_context", MagicMock(return_value=workflow_context)
    )
    monkeypatch.setattr(app.BROWSER_MANAGER, "get_for_workflow_run", MagicMock(return_value=browser_state))
    monkeypatch.setattr(app.DATABASE.observer, "update_workflow_run_block", rows.update)
    monkeypatch.setattr(app.DATABASE.tasks, "update_step", AsyncMock(return_value=SimpleNamespace(order=0)))
    monkeypatch.setattr(app.DATABASE.tasks, "update_task", update_task)
    monkeypatch.setattr(app.DATABASE.tasks, "get_step", AsyncMock(return_value=SimpleNamespace(step_id="stp")))
    monkeypatch.setattr(app.DATABASE.tasks, "get_task", AsyncMock(return_value=make_task(now, organization)))
    monkeypatch.setattr(app.DATABASE.organizations, "get_organization", AsyncMock(return_value=organization))
    monkeypatch.setattr(app.DATABASE.workflows, "get_workflow", AsyncMock(return_value=workflow))
    monkeypatch.setattr(app.DATABASE.workflow_runs, "get_workflow_run", AsyncMock(return_value=workflow))
    monkeypatch.setattr(app.AGENT_FUNCTION, "post_cache_step_execution", billed)
    monkeypatch.setattr(app.ARTIFACT_MANAGER, "flush_step_archive", AsyncMock())
    monkeypatch.setattr(app.STORAGE, "get_current_attempt_downloaded_files", AsyncMock(return_value=[]))
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "get_recent_task_screenshot_artifacts", AsyncMock(return_value=[]))
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "get_recent_workflow_screenshot_artifacts", AsyncMock(return_value=[]))
    with skyvern_context.scoped(run_context):
        yield SimpleNamespace(
            rows=rows,
            manager=manager,
            browser_state=browser_state,
            browser_context=browser_context,
            billed=billed,
            workflow_context=workflow_context,
            run_context=run_context,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("child", ["native_loss", "ordinary_error", "healthy"])
@pytest.mark.parametrize("helper", ["run_task", "action", "login"])
async def test_cached_loop_stops_at_the_child_that_lost_its_browser(
    cached_run: SimpleNamespace, helper: str, child: str
) -> None:
    cached_helper = getattr(script_service, helper)
    opened: list[int] = []

    async def open_page(page: object, context: object) -> dict[str, str]:
        opened.append(len(opened))
        if child == "native_loss":
            cached_run.browser_state._on_browser_context_closed(cached_run.browser_context)
            raise PlaywrightError("Target page, context or browser has been closed")
        if child == "ordinary_error":
            raise PlaywrightError("Timeout 30000ms exceeded.")
        return {"opened": "yes"}

    cached_run.manager.set_cached_fn("open_page", open_page)
    after_child: list[int] = []

    async def generated_loop() -> None:
        # Generated code leaves an abandoned loop to the garbage collector; closing it here keeps that late
        # finalizer off the test's closing event loop.
        async with contextlib.aclosing(script_service.loop([1, 2], label="rows")) as rows:
            async for _ in rows:
                await cached_helper(prompt="Open the page", label="open_page", cache_key="open_page")
                after_child.append(len(after_child))
                await script_service.wait(0, label="pause")

    if child == "native_loss":
        with pytest.raises(MissingBrowserState):
            await generated_loop()
    else:
        await generated_loop()

    open_rows = cached_run.rows.labelled("open_page")
    (loop_row,) = cached_run.rows.labelled("rows")
    if child == "native_loss":
        assert opened == [0]
        assert after_child == []
        assert cached_run.rows.labelled("pause") == []
        assert loop_row["status"] != BlockStatus.completed
        assert [row["status"] for row in open_rows] == [BlockStatus.failed]
        assert open_rows[0]["error_codes"] == [BROWSER_CONNECTION_LOST_ERROR_CODE]
        assert open_rows[0]["output"]
        cached_run.billed.assert_awaited_once()
        assert cached_run.workflow_context.get_block_outcome("open_page").error_codes == [
            BROWSER_CONNECTION_LOST_ERROR_CODE
        ]
        assert cached_run.run_context.prompt is None
    else:
        expected = BlockStatus.failed if child == "ordinary_error" else BlockStatus.completed
        assert opened == [0, 1]
        assert len(cached_run.rows.labelled("pause")) == 2
        assert loop_row["status"] == BlockStatus.completed
        assert [row["status"] for row in open_rows] == [expected, expected]
        assert [row["error_codes"] for row in open_rows] == [None, None]


_UNCACHED_CHILDREN: dict[str, tuple[type[Block], Callable[[], Awaitable[object]]]] = {
    "run_task": (NavigationBlock, lambda: script_service.run_task(prompt="Open the page", label="open_page")),
    "goto": (UrlBlock, lambda: script_service.goto("https://93.184.216.34/next", label="go")),
    "action": (ActionBlock, lambda: script_service.action(prompt="Click it", label="click")),
    "login": (LoginBlock, lambda: script_service.login(prompt="Sign in", label="sign_in")),
    "extract": (ExtractionBlock, lambda: script_service.extract(prompt="Read it", label="read")),
    "prompt": (TextPromptBlock, lambda: script_service.prompt(prompt="Summarize", label="summary")),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("browser_lost", [True, False], ids=["browser_lost", "browser_alive"])
@pytest.mark.parametrize("helper", sorted(_UNCACHED_CHILDREN))
async def test_uncached_child_that_failed_after_browser_loss_ends_the_script(
    cached_run: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, helper: str, browser_lost: bool
) -> None:
    block_type, run_child = _UNCACHED_CHILDREN[helper]
    failed = BlockResult(
        success=False,
        output_parameter=make_block_output_parameter("child_output"),
        status=BlockStatus.failed,
        failure_reason="Failed",
    )
    monkeypatch.setattr(block_type, "execute_safe", AsyncMock(return_value=failed))
    fallback_page = cached_run.manager.ensure_run_context().page
    fallback_page.goto = AsyncMock()
    if browser_lost:
        cached_run.browser_state._on_browser_context_closed(cached_run.browser_context)

    if browser_lost:
        with pytest.raises(MissingBrowserState):
            await run_child()
    else:
        await run_child()

    fallback_page.goto.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "seam", "iterations", "conditions"),
    [
        ("for", "before_the_loop", [], 0),
        ("while", "before_the_loop", [], 0),
        ("for", "during_the_baseline", [], 0),
        ("while", "during_the_baseline", [], 1),
        ("while", "during_the_condition", [], 1),
        ("for", "in_a_body", [0], 0),
        ("while", "in_a_body", [0], 1),
        ("for", "in_the_final_body", [0, 1], 0),
        ("for", "browser_alive", [0, 1], 0),
        ("while", "browser_alive", [0, 1], 3),
    ],
)
async def test_no_loop_starts_iterates_or_completes_after_its_run_lost_the_browser(
    cached_run: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    seam: str,
    iterations: list[int],
    conditions: int,
) -> None:
    def lose() -> None:
        cached_run.browser_state._on_browser_context_closed(cached_run.browser_context)

    evaluated: list[bool] = []

    async def evaluate_condition(*_: object, **__: object) -> bool:
        evaluated.append(seam != "during_the_condition" and len(evaluated) < 2)
        if seam == "during_the_condition":
            lose()
        return evaluated[-1]

    async def baseline(**_: object) -> list[object]:
        if seam == "during_the_baseline":
            lose()
        return []

    monkeypatch.setattr(WhileLoopBlock, "_evaluate_condition", evaluate_condition)
    monkeypatch.setattr(app.STORAGE, "get_current_attempt_downloaded_files", baseline)
    if seam == "before_the_loop":
        lose()
    if kind == "for":
        rows = script_service.loop([1, 2], label="rows")
    else:
        rows = script_service.while_loop("{{ true }}", label="rows")
    seen: list[int] = []
    lost = seam != "browser_alive"

    with pytest.raises(MissingBrowserState) if lost else contextlib.nullcontext():
        async with contextlib.aclosing(rows) as items:
            async for _ in items:
                seen.append(len(seen))
                cached_run.run_context.loop_output_values[-1].append(f"row {len(seen)}")
                if seam == "in_a_body" or (seam == "in_the_final_body" and len(seen) == 2):
                    lose()

    assert seen == iterations
    assert len(evaluated) == conditions
    loop_rows = cached_run.rows.labelled("rows")
    if seam == "before_the_loop":
        assert loop_rows == []
        return
    (loop_row,) = loop_rows
    assert loop_row["output"] == ([[f"row {n + 1}"] for n in iterations] or None)
    assert loop_row["status"] == (BlockStatus.failed if lost else BlockStatus.completed)
    assert loop_row["error_codes"] == ([BROWSER_CONNECTION_LOST_ERROR_CODE] if lost else None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("helper", "seam"),
    [
        (helper, seam)
        for helper in ("conditional", "run_task", "action", "login", "extract", "download")
        for seam in ("lost_during_the_wait", "lost_during_setup", "browser_alive")
        if (helper, seam) != ("download", "browser_alive")
    ],
)
async def test_no_cached_statement_runs_after_its_run_lost_the_browser(
    cached_run: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, helper: str, seam: str
) -> None:
    def lose() -> None:
        cached_run.browser_state._on_browser_context_closed(cached_run.browser_context)

    async def sleep(_: float) -> None:
        if seam == "lost_during_the_wait":
            lose()

    create_row = script_service._create_workflow_block_run_and_task

    async def create_row_then_lose(*args: Any, **kwargs: Any) -> tuple[str, str | None, str | None]:
        created = await create_row(*args, **kwargs)
        if seam == "lost_during_setup" and kwargs.get("label") == helper:
            lose()
        return created

    evaluated: list[str] = []

    async def cached_statement(page: object, context: object) -> dict[str, Any]:
        evaluated.append(helper)
        return {"next_block_label": None, "branch_index": 0}

    monkeypatch.setattr(script_service, "asyncio", ScopedAsyncio(sleep=sleep))
    monkeypatch.setattr(script_service, "_create_workflow_block_run_and_task", create_row_then_lose)
    monkeypatch.setattr(script_service, "CACHED_DOWNLOAD_NO_FILE_GRACE_SECONDS", 0)
    cached_run.manager.set_cached_fn(helper, cached_statement)
    statements: list[str] = []

    async def generated_function() -> None:
        await script_service.wait(1, label="pause")
        if helper == "conditional":
            await script_service.conditional(label=helper)
        else:
            await getattr(script_service, helper)(prompt="Read the page", label=helper, cache_key=helper)
        statements.append("after")

    lost = seam != "browser_alive"
    with pytest.raises(MissingBrowserState) if lost else contextlib.nullcontext():
        await generated_function()

    assert evaluated == ([] if lost else [helper])
    assert statements == ([] if lost else ["after"])
    rows = [(row["status"], row["error_codes"]) for row in cached_run.rows.labelled(helper)]
    if seam == "lost_during_the_wait":
        assert rows == []
    elif seam == "lost_during_setup":
        assert rows == [(BlockStatus.failed, [BROWSER_CONNECTION_LOST_ERROR_CODE])]
    else:
        assert rows == [(BlockStatus.completed, None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("lost", [True, False], ids=["lost_during_setup", "browser_alive"])
async def test_cached_request_is_not_sent_after_a_loss_during_its_setup(
    cached_run: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, lost: bool
) -> None:
    validated = await script_service._validate_and_get_output_parameter()

    async def validate_then_lose(*_: object) -> script_service.BlockValidationOutput:
        if lost:
            cached_run.browser_state._on_browser_context_closed(cached_run.browser_context)
        return validated

    @contextlib.asynccontextmanager
    async def admit(_: str) -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(status=WorkflowRunStatus.running)

    sent = AsyncMock(return_value=BlockResult(success=True, output_parameter=validated.output_parameter))
    monkeypatch.setattr(script_service, "_validate_and_get_output_parameter", validate_then_lose)
    monkeypatch.setattr(
        app.DATABASE.observer,
        "create_workflow_run_block",
        AsyncMock(side_effect=lambda **kw: SimpleNamespace(workflow_run_block_id=cached_run.rows.create(kw["label"]))),
    )
    monkeypatch.setattr(app.DATABASE.workflow_runs, "admit_workflow_run_block_dispatch", admit)
    monkeypatch.setattr(HttpRequestBlock, "execute", sent)

    with pytest.raises(MissingBrowserState) if lost else contextlib.nullcontext():
        await script_service.http_request(method="POST", url="https://example.com/orders", label="rows")

    assert sent.await_count == (0 if lost else 1)
    assert [row["error_codes"] for row in cached_run.rows.labelled("rows")] == [
        [BROWSER_CONNECTION_LOST_ERROR_CODE] if lost else None
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("browser_lost", [True, False], ids=["browser_lost_during_validation", "ordinary_failure"])
async def test_cached_page_validate_after_browser_loss_ends_the_script(
    cached_run: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, browser_lost: bool
) -> None:
    async def validate_page(*_args: object, **_kwargs: object) -> BlockResult:
        if browser_lost:
            cached_run.browser_state._on_browser_context_closed(cached_run.browser_context)
        return BlockResult(
            success=False,
            output_parameter=make_block_output_parameter("check_output"),
            status=BlockStatus.failed,
            failure_reason="The order is not placed",
        )

    monkeypatch.setattr(ValidationBlock, "execute_safe", validate_page)
    scraped_page = SimpleNamespace(_browser_state=cached_run.browser_state)
    raw_page = SimpleNamespace(url="https://example.com/checkout")
    with patch("skyvern.core.script_generations.skyvern_page.Page.__init__", return_value=None):
        page = ScriptSkyvernPage(scraped_page=scraped_page, page=raw_page, ai=RealSkyvernPageAi(scraped_page, raw_page))
    statements: list[object] = []

    async def generated_function() -> None:
        statements.append(await page.validate("Is the order placed?"))
        statements.append("next statement")

    with pytest.raises(MissingBrowserState) if browser_lost else contextlib.nullcontext():
        await generated_function()

    assert statements == ([] if browser_lost else [False, "next statement"])


@pytest.mark.asyncio
@pytest.mark.parametrize("browser_lost", [True, False], ids=["browser_lost", "browser_alive"])
async def test_uncached_download_failure_after_browser_loss_raises_the_typed_refusal(
    cached_run: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, browser_lost: bool
) -> None:
    failed = BlockResult(
        success=False,
        output_parameter=make_block_output_parameter("download_output"),
        status=BlockStatus.failed,
        failure_reason="Failed",
    )
    monkeypatch.setattr(FileDownloadBlock, "execute_safe", AsyncMock(return_value=failed))
    if browser_lost:
        cached_run.browser_state._on_browser_context_closed(cached_run.browser_context)

    with pytest.raises(MissingBrowserState if browser_lost else ScriptTerminationException):
        await script_service.download(prompt="Download the invoice", label="invoice")


@pytest.mark.asyncio
async def test_sftp_source_download_bypasses_cached_browser_download(
    cached_run: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    cached_lookup = MagicMock(side_effect=AssertionError("SFTP sources must bypass cached browser downloads"))
    monkeypatch.setattr(script_service.script_run_context_manager, "get_cached_fn", cached_lookup)
    executed_blocks: list[FileDownloadBlock] = []

    async def execute_source(block: FileDownloadBlock, **_: object) -> BlockResult:
        executed_blocks.append(block)
        return BlockResult(
            success=True,
            output_parameter=make_block_output_parameter("download_output"),
            status=BlockStatus.completed,
        )

    monkeypatch.setattr(FileDownloadBlock, "execute_safe", execute_source)

    await script_service.download(
        prompt="Read the report",
        label="sftp_source",
        sftp_source_path="/reports/report.csv",
        sftp_host="sftp.example.test",
        sftp_port=2222,
        sftp_username="source_user",
        sftp_password="source_password",
        sftp_host_key="ssh-ed25519 AAAA",
    )

    cached_lookup.assert_not_called()
    assert len(executed_blocks) == 1
    assert executed_blocks[0].sftp_source_path == "/reports/report.csv"
    assert executed_blocks[0].sftp_host == "sftp.example.test"
    assert executed_blocks[0].sftp_port == 2222
    assert executed_blocks[0].sftp_username == "source_user"
    assert executed_blocks[0].sftp_password == "source_password"
    assert executed_blocks[0].sftp_host_key == "ssh-ed25519 AAAA"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("complete_if_empty", "expected_status", "expected_reason"),
    [
        (True, BlockStatus.completed, None),
        (False, BlockStatus.terminated, "No iterable value found for the loop block"),
    ],
)
async def test_cached_loop_with_no_values_records_its_outcome(
    monkeypatch: pytest.MonkeyPatch,
    complete_if_empty: bool,
    expected_status: BlockStatus,
    expected_reason: str | None,
) -> None:
    context = WorkflowRunContext(
        workflow_title="test",
        workflow_id="wf",
        workflow_permanent_id="wpid",
        workflow_run_id="wr_cached",
        aws_client=AsyncMock(),
    )
    run_context = skyvern_context.SkyvernContext(
        organization_id="o_cached", workflow_id="wf", workflow_run_id="wr_cached", script_mode=True
    )
    monkeypatch.setattr(app.WORKFLOW_CONTEXT_MANAGER, "get_workflow_run_context", MagicMock(return_value=context))
    monkeypatch.setattr(
        script_service, "_create_workflow_block_run_and_task", AsyncMock(return_value=("wrb_rows", None, None))
    )
    monkeypatch.setattr(
        script_service,
        "_validate_and_get_output_parameter",
        AsyncMock(
            return_value=script_service.BlockValidationOutput(
                context=run_context,
                label="rows",
                output_parameter=make_block_output_parameter("rows_output"),
                input_parameters=[],
                workflow=MagicMock(),
                workflow_id="wf",
                workflow_run_id="wr_cached",
                organization_id="o_cached",
            )
        ),
    )
    monkeypatch.setattr(ForLoopBlock, "get_values_from_loop_variable_reference", AsyncMock(return_value=[]))

    async def drain() -> None:
        async for _ in script_service.loop([], complete_if_empty=complete_if_empty, label="rows"):
            pass

    if complete_if_empty:
        await drain()
    else:
        with pytest.raises(Exception, match="No iterable value"):
            await drain()

    assert context.get_block_outcome("rows") == BlockOutcome(
        status=expected_status, error_codes=[], failure_reason=expected_reason
    )


@pytest.mark.asyncio
async def test_cached_outcome_survives_output_parameter_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    context = WorkflowRunContext(
        workflow_title="test",
        workflow_id="wf",
        workflow_permanent_id="wpid",
        workflow_run_id="wr_cached",
        aws_client=AsyncMock(),
    )
    run_context = skyvern_context.SkyvernContext(
        organization_id="o_cached", workflow_id="wf", workflow_run_id="wr_cached", script_mode=True
    )
    monkeypatch.setattr(script_service.skyvern_context, "current", lambda: run_context)
    monkeypatch.setattr(app.WORKFLOW_CONTEXT_MANAGER, "get_workflow_run_context", MagicMock(return_value=context))
    monkeypatch.setattr(
        app.DATABASE.observer,
        "update_workflow_run_block",
        AsyncMock(return_value=SimpleNamespace(label="cached", error_codes=["stale"])),
    )
    monkeypatch.setattr(
        script_service, "_record_output_parameter_value", AsyncMock(side_effect=RuntimeError("Output unavailable"))
    )

    await script_service._update_workflow_block(
        "wrb_cached", BlockStatus.failed, label="cached", failure_reason="Failed", error_codes=["picked"]
    )

    assert context.get_block_outcome("cached") == BlockOutcome(
        status=BlockStatus.failed, error_codes=["picked"], failure_reason="Failed"
    )
    assert app.DATABASE.observer.update_workflow_run_block.await_args.kwargs["error_codes"] is None


@pytest.mark.asyncio
async def test_outcome_record_failure_does_not_skip_cached_output(monkeypatch: pytest.MonkeyPatch) -> None:
    run_context = skyvern_context.SkyvernContext(
        organization_id="o_cached", workflow_id="wf", workflow_run_id="wr_cached", script_mode=True
    )
    monkeypatch.setattr(script_service.skyvern_context, "current", lambda: run_context)
    workflow_context = MagicMock()
    workflow_context.record_block_outcome.side_effect = RuntimeError("Outcome unavailable")
    monkeypatch.setattr(
        app.WORKFLOW_CONTEXT_MANAGER, "get_workflow_run_context", MagicMock(return_value=workflow_context)
    )
    monkeypatch.setattr(
        app.DATABASE.observer,
        "update_workflow_run_block",
        AsyncMock(return_value=SimpleNamespace(label="cached", error_codes=[])),
    )
    record_output = AsyncMock()
    monkeypatch.setattr(script_service, "_record_output_parameter_value", record_output)

    await script_service._update_workflow_block("wrb_cached", BlockStatus.completed, label="cached")

    record_output.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_script_user_error_call_reads_the_tasks_recorded_solves_in_its_one_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 1)
    task = make_task(now, make_organization(now), error_code_mapping={"BOT_WALL": "A bot check blocked the form"})
    task.started_at = now
    scraped = MagicMock(url="https://site.test/form", screenshots=[], build_element_tree=lambda *_: "<form/>")
    run_context = MagicMock()
    run_context.page.scraped_page.refresh = AsyncMock(return_value=scraped)
    monkeypatch.setattr(script_service.script_run_context_manager, "ensure_run_context", lambda: run_context)
    prompts: list[str] = []

    async def handler(*, prompt: str, **_: Any) -> dict[str, Any]:
        prompts.append(prompt)
        return {"errors": [{"error_code": "BOT_WALL", "reasoning": "unsolved check", "confidence_float": 0.8}]}

    monkeypatch.setattr(app, "EXTRACTION_LLM_API_HANDLER", handler)
    receipt = ChallengeSolveReceipt(
        task.task_id, datetime(2026, 8, 1, 0, 1, tzinfo=timezone.utc), "hcaptcha", "unsolved", "unknown", task.task_id
    )

    with skyvern_context.scoped(SkyvernContext(task_id=task.task_id)) as context:
        context.challenge_solve_receipts.append(receipt)
        errors = await script_service._detect_user_defined_errors(
            task, MagicMock(), "wr_1", task.error_code_mapping or {}
        )

    [prompt] = prompts
    assert '"service": "hcaptcha", "outcome": "unsolved", "page_state": "unknown", "unresolved": true' in prompt
    assert [error.error_code for error in errors] == ["BOT_WALL"]
