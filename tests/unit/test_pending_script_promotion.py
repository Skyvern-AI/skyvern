"""
Tests for pending-script promotion and per-block mint convergence (SKY-13659).

Defect A: the end-of-run finalize looked up only published scripts, missed the
pending script the same run minted per-block, and created a duplicate script row
with identical content.

Defect B: the per-block pending-mint path counted non-cacheable blocks (goto/code)
as "missing" forever, so code/goto-only workflows regenerated the full script after
every block instead of once.
"""

import ast
import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import libcst as cst
import pytest

from skyvern.core.script_generations.generate_script import _build_block_statement
from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.experimentation.providers import BaseExperimentationProvider
from skyvern.forge.sdk.workflow.models.block import ExtractionBlock, TaskBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.schemas.run_enums import RunEngine
from skyvern.schemas.scripts import ScriptStatus
from skyvern.schemas.workflows import BlockStatus, BlockType
from skyvern.services import workflow_script_service
from skyvern.services import workflow_service as workflow_service_module
from skyvern.webeye.actions.actions import ClickAction
from tests.unit.test_agent_task_v3 import _make_block, _run_execute_step_gate


def make_workflow(block_types: list[BlockType]) -> SimpleNamespace:
    blocks = [SimpleNamespace(block_type=block_type, label=f"block_{i}") for i, block_type in enumerate(block_types)]
    return SimpleNamespace(
        organization_id="o_1",
        workflow_id="w_1",
        workflow_permanent_id="wpid_1",
        cache_key="",
        generate_script_on_terminal=False,
        workflow_definition=SimpleNamespace(blocks=blocks),
    )


def make_workflow_run() -> SimpleNamespace:
    return SimpleNamespace(workflow_run_id="wr_1", organization_id="o_1", code_gen=None, run_with=None)


def make_scripts_db(pending_row: SimpleNamespace | None) -> MagicMock:
    scripts = MagicMock()
    scripts.get_workflow_script = AsyncMock(return_value=pending_row)
    scripts.create_workflow_script = AsyncMock()
    scripts.update_workflow_script_status = AsyncMock()

    async def create_script(
        organization_id: str,
        run_id: str | None = None,
        script_id: str | None = None,
        version: int | None = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            script_id=script_id or "s_new",
            script_revision_id=f"sr_{script_id or 'new'}_v{version or 1}",
            version=version or 1,
        )

    scripts.create_script = AsyncMock(side_effect=create_script)
    scripts.get_script = AsyncMock(
        return_value=SimpleNamespace(script_id="s_pending", script_revision_id="sr_pending", version=3)
    )
    scripts.get_script_version_stats = AsyncMock(return_value={"s_pending": (3, 3)})
    scripts.get_script_files = AsyncMock(return_value=["main.py"])
    scripts.get_script_blocks_by_script_revision_id = AsyncMock(return_value=["block"])
    scripts.soft_delete_script_by_revision = AsyncMock()
    return scripts


class TestCacheableMissingLabels:
    def test_excludes_non_cacheable_block_types(self) -> None:
        blocks = [
            {"label": "open_target", "block_type": BlockType.GOTO_URL},
            {"label": "smoke_page_evaluate", "block_type": BlockType.CODE},
            {"label": "fill_form", "block_type": BlockType.TASK},
        ]
        missing = workflow_script_service.cacheable_missing_labels(blocks, cached_labels=set())
        assert missing == {"fill_form"}

    def test_accepts_plain_string_block_types(self) -> None:
        blocks = [
            {"label": "open_target", "block_type": "goto_url"},
            {"label": "fill_form", "block_type": "task"},
        ]
        missing = workflow_script_service.cacheable_missing_labels(blocks, cached_labels=set())
        assert missing == {"fill_form"}

    def test_empty_when_cacheable_blocks_already_cached(self) -> None:
        blocks = [{"label": "fill_form", "block_type": BlockType.TASK}]
        assert workflow_script_service.cacheable_missing_labels(blocks, cached_labels={"fill_form"}) == set()

    def test_excludes_extraction_blocks_with_export_enabled(self) -> None:
        # SKY-15396: the generated script's cached replay only carries
        # prompt/schema/url/model, so caching an export-enabled extraction
        # block would silently skip the Parquet export on every later run.
        blocks = [
            {"label": "plain_extract", "block_type": BlockType.EXTRACTION, "export_enabled": False},
            {"label": "export_extract", "block_type": BlockType.EXTRACTION, "export_enabled": True},
        ]
        missing = workflow_script_service.cacheable_missing_labels(blocks, cached_labels=set())
        assert missing == {"plain_extract"}

    def test_extraction_without_export_enabled_key_is_still_cacheable(self) -> None:
        # Block dicts predating this field (or non-extraction types) have no
        # export_enabled key at all -- must default to cacheable, not excluded.
        blocks = [{"label": "plain_extract", "block_type": BlockType.EXTRACTION}]
        assert workflow_script_service.cacheable_missing_labels(blocks, cached_labels=set()) == {"plain_extract"}


class TestIsBlockTypeCacheable:
    def test_accepts_a_real_block_model_instance_not_just_dicts(self) -> None:
        # service.py's mint/regen/run-dispatch call sites all hold typed Block
        # instances (workflow.workflow_definition.blocks), not dicts.
        now = datetime.now(UTC)
        output_parameter = OutputParameter(
            output_parameter_id="op_1", key="out", workflow_id="w_1", created_at=now, modified_at=now
        )
        plain = ExtractionBlock(label="plain", output_parameter=output_parameter, data_extraction_goal="g")
        exporting = ExtractionBlock(
            label="exporting", output_parameter=output_parameter, data_extraction_goal="g", export_enabled=True
        )
        non_cacheable = SimpleNamespace(block_type=BlockType.WAIT)

        assert workflow_script_service.is_block_type_cacheable(plain) is True
        assert workflow_script_service.is_block_type_cacheable(exporting) is False
        assert workflow_script_service.is_block_type_cacheable(non_cacheable) is False

    @pytest.mark.parametrize("loop_type", [BlockType.FOR_LOOP, BlockType.WHILE_LOOP])
    @pytest.mark.parametrize(
        "engine_only_child",
        [
            {"label": "stop", "block_type": BlockType.TERMINATE, "reason": "missing"},
            {"label": "search", "block_type": BlockType.WEB_SEARCH, "query": "q"},
            {
                "label": "route",
                "block_type": BlockType.CONDITIONAL,
                "branch_conditions": [
                    {"criteria": {"expression": "{{ current_value }}"}, "next_block_label": "fill"},
                    {"is_default": True, "next_block_label": None},
                ],
            },
        ],
        ids=["terminate", "web_search", "conditional"],
    )
    def test_loop_containing_an_engine_only_block_is_not_cacheable(
        self, loop_type: BlockType, engine_only_child: dict
    ) -> None:
        task = {"label": "fill", "block_type": BlockType.TASK}
        direct = {"label": "outer", "block_type": loop_type, "loop_blocks": [task, engine_only_child]}
        nested = {
            "label": "outer",
            "block_type": loop_type,
            "loop_blocks": [
                task,
                {"label": "inner", "block_type": BlockType.FOR_LOOP, "loop_blocks": [engine_only_child]},
            ],
        }
        plain = {"label": "outer", "block_type": loop_type, "loop_blocks": [task]}

        assert workflow_script_service.is_block_type_cacheable(direct) is False
        assert workflow_script_service.is_block_type_cacheable(nested) is False
        assert workflow_script_service.is_block_type_cacheable(plain) is True

    @pytest.mark.parametrize("child_type", sorted(set(BlockType) - {BlockType.FOR_LOOP, BlockType.WHILE_LOOP}))
    def test_loop_is_cacheable_only_when_codegen_emits_a_call_for_its_child(self, child_type: BlockType) -> None:
        # A child that codegen renders as a bare string or comment would be silently skipped by a cached loop.
        child = {"label": "child", "block_type": child_type}
        code = cst.Module(body=[_build_block_statement(child)]).code
        emits_a_call = any(isinstance(node, ast.Await) for node in ast.walk(ast.parse(code)))
        loop = {"label": "outer", "block_type": BlockType.FOR_LOOP, "loop_blocks": [child]}

        assert workflow_script_service.is_block_type_cacheable(loop) is emits_a_call


class TestPendingMintSkipsNonCacheableWorkflows:
    async def _run_hook(self, workflow: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> list:
        calls: list = []

        async def record(wf: object, run: object) -> None:
            calls.append(run)

        stub_self = SimpleNamespace(_do_generate_pending_script=record)
        block_result = SimpleNamespace(status=BlockStatus.completed)
        skyvern_context.set(SkyvernContext())
        try:
            await WorkflowService._generate_pending_script_for_block(
                stub_self, workflow, make_workflow_run(), block_result
            )
            await asyncio.sleep(0)
        finally:
            skyvern_context.reset()
        return calls

    @pytest.mark.asyncio
    async def test_skips_when_workflow_has_no_cacheable_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        workflow = make_workflow([BlockType.GOTO_URL, BlockType.CODE, BlockType.CODE])
        calls = await self._run_hook(workflow, monkeypatch)
        assert calls == []

    @pytest.mark.asyncio
    async def test_still_mints_when_workflow_has_cacheable_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        workflow = make_workflow([BlockType.GOTO_URL, BlockType.TASK])
        calls = await self._run_hook(workflow, monkeypatch)
        assert len(calls) == 1


class TestRecordWorkflowScriptMapping:
    @pytest.mark.asyncio
    async def test_publish_promotes_existing_pending_row_for_same_script(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pending_row = SimpleNamespace(workflow_script_id="ws_1", script_id="s_pending")
        scripts = make_scripts_db(pending_row)
        monkeypatch.setattr(app, "DATABASE", SimpleNamespace(scripts=scripts), raising=False)

        await workflow_script_service._record_workflow_script_mapping(
            workflow=make_workflow([BlockType.CODE]),
            workflow_run=make_workflow_run(),
            script=SimpleNamespace(script_id="s_pending", script_revision_id="sr_pending", version=1),
            rendered_cache_key_value="default:site",
            pending=False,
        )

        scripts.update_workflow_script_status.assert_awaited_once_with(
            workflow_script_id="ws_1",
            organization_id="o_1",
            status=ScriptStatus.published,
        )
        scripts.create_workflow_script.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_publish_creates_row_when_no_pending_exists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        scripts = make_scripts_db(pending_row=None)
        monkeypatch.setattr(app, "DATABASE", SimpleNamespace(scripts=scripts), raising=False)

        await workflow_script_service._record_workflow_script_mapping(
            workflow=make_workflow([BlockType.CODE]),
            workflow_run=make_workflow_run(),
            script=SimpleNamespace(script_id="s_new", script_revision_id="sr_new", version=1),
            rendered_cache_key_value="default:site",
            pending=False,
        )

        scripts.create_workflow_script.assert_awaited_once()
        assert scripts.create_workflow_script.await_args.kwargs["status"] == ScriptStatus.published
        scripts.update_workflow_script_status.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_publish_creates_row_when_pending_points_at_different_script(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pending_row = SimpleNamespace(workflow_script_id="ws_1", script_id="s_other")
        scripts = make_scripts_db(pending_row)
        monkeypatch.setattr(app, "DATABASE", SimpleNamespace(scripts=scripts), raising=False)

        await workflow_script_service._record_workflow_script_mapping(
            workflow=make_workflow([BlockType.CODE]),
            workflow_run=make_workflow_run(),
            script=SimpleNamespace(script_id="s_new", script_revision_id="sr_new", version=1),
            rendered_cache_key_value="default:site",
            pending=False,
        )

        scripts.create_workflow_script.assert_awaited_once()
        scripts.update_workflow_script_status.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pending_creates_row_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        scripts = make_scripts_db(pending_row=None)
        monkeypatch.setattr(app, "DATABASE", SimpleNamespace(scripts=scripts), raising=False)

        await workflow_script_service._record_workflow_script_mapping(
            workflow=make_workflow([BlockType.CODE]),
            workflow_run=make_workflow_run(),
            script=SimpleNamespace(script_id="s_pending", script_revision_id="sr_pending", version=1),
            rendered_cache_key_value="default:site",
            pending=True,
        )
        scripts.create_workflow_script.assert_awaited_once()
        assert scripts.create_workflow_script.await_args.kwargs["status"] == ScriptStatus.pending

    @pytest.mark.asyncio
    async def test_pending_does_not_duplicate_existing_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pending_row = SimpleNamespace(workflow_script_id="ws_1", script_id="s_pending")
        scripts = make_scripts_db(pending_row)
        monkeypatch.setattr(app, "DATABASE", SimpleNamespace(scripts=scripts), raising=False)

        await workflow_script_service._record_workflow_script_mapping(
            workflow=make_workflow([BlockType.CODE]),
            workflow_run=make_workflow_run(),
            script=SimpleNamespace(script_id="s_pending", script_revision_id="sr_pending", version=1),
            rendered_cache_key_value="default:site",
            pending=True,
        )
        scripts.create_workflow_script.assert_not_awaited()


class TestPendingMintUsesOriginalRevision:
    """Per-block mints must keep writing to their original pending revision.

    ``script_files`` is unique on ``(script_revision_id, file_path)``. A straggler
    must never resolve the newer published revision for the same script id.
    """

    @pytest.mark.asyncio
    async def test_uses_context_revision_after_finalize_mints_newer_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scripts = MagicMock()
        original_revision = SimpleNamespace(script_id="s_pending", script_revision_id="sr_pending", version=1)
        scripts.get_script_revision = AsyncMock(return_value=original_revision)
        scripts.get_script = AsyncMock(
            return_value=SimpleNamespace(script_id="s_pending", script_revision_id="sr_published", version=2)
        )
        scripts.get_workflow_script = AsyncMock(return_value=None)
        scripts.create_script = AsyncMock()
        monkeypatch.setattr(app, "DATABASE", SimpleNamespace(scripts=scripts), raising=False)
        monkeypatch.setattr(
            workflow_script_service,
            "get_workflow_script",
            AsyncMock(return_value=(None, "default:site", False)),
        )
        generate_mock = AsyncMock()
        monkeypatch.setattr(workflow_script_service, "generate_workflow_script", generate_mock)

        skyvern_context.set(SkyvernContext(script_id="s_pending", script_revision_id="sr_pending"))
        try:
            await workflow_script_service.generate_or_update_pending_workflow_script(
                workflow_run=make_workflow_run(),
                workflow=make_workflow([BlockType.TASK]),
            )
        finally:
            skyvern_context.reset()

        minted_script = generate_mock.await_args.kwargs["script"]
        assert minted_script.script_revision_id == "sr_pending"
        scripts.get_script_revision.assert_awaited_once_with(
            script_revision_id="sr_pending",
            organization_id="o_1",
        )
        scripts.get_script.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_does_not_recreate_pending_mapping_after_finalize_promotes_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scripts = MagicMock()
        published_mapping = SimpleNamespace(
            workflow_script_id="ws_1",
            script_id="s_pending",
            status=ScriptStatus.published,
        )

        async def get_mapping(**kwargs: object) -> SimpleNamespace | None:
            if kwargs["statuses"] == [ScriptStatus.pending, ScriptStatus.published]:
                return published_mapping
            return None

        scripts.get_workflow_script = AsyncMock(side_effect=get_mapping)
        scripts.create_workflow_script = AsyncMock()
        monkeypatch.setattr(app, "DATABASE", SimpleNamespace(scripts=scripts), raising=False)

        await workflow_script_service._record_workflow_script_mapping(
            workflow=make_workflow([BlockType.TASK]),
            workflow_run=make_workflow_run(),
            script=SimpleNamespace(script_id="s_pending", script_revision_id="sr_pending", version=1),
            rendered_cache_key_value="default:site",
            pending=True,
        )

        scripts.create_workflow_script.assert_not_awaited()


def run_blocks_db(*engines: RunEngine | None) -> SimpleNamespace:
    async def has_block_on_engine(*, workflow_run_id: str, engine: RunEngine, organization_id: str) -> bool:
        return engine in engines

    return SimpleNamespace(workflow_run_has_block_on_engine=has_block_on_engine)


class BlockRowsByTask:
    """Block-row engines keyed by task, written by the dispatch fallback and read by the mint guard."""

    def __init__(self, **engines: RunEngine) -> None:
        self.engines = dict(engines)

    async def set_workflow_run_block_engine_by_task_id(
        self, task_id: str, engine: RunEngine, organization_id: str | None = None
    ) -> bool:
        self.engines[task_id] = engine
        return True

    async def workflow_run_has_block_on_engine(
        self, *, workflow_run_id: str, engine: RunEngine, organization_id: str | None
    ) -> bool:
        return engine in self.engines.values()


class TestFinalizeReusesPendingScript:
    def _patch_common(
        self,
        monkeypatch: pytest.MonkeyPatch,
        scripts: MagicMock,
        *engines: RunEngine | None,
        observer: BlockRowsByTask | None = None,
    ) -> AsyncMock:
        database = SimpleNamespace(scripts=scripts, observer=observer or run_blocks_db(*engines))
        monkeypatch.setattr(app, "DATABASE", database, raising=False)
        monkeypatch.setattr(app, "ARTIFACT_MANAGER", SimpleNamespace(upload_aiotasks_map={}), raising=False)
        monkeypatch.setattr(
            workflow_script_service,
            "get_workflow_script",
            AsyncMock(return_value=(None, "default:site", False)),
        )
        generate_mock = AsyncMock()
        monkeypatch.setattr(workflow_script_service, "generate_workflow_script", generate_mock)
        return generate_mock

    @pytest.mark.asyncio
    async def test_first_run_finalize_reuses_pending_script_id_without_minting_duplicate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pending_row = SimpleNamespace(workflow_script_id="ws_1", script_id="s_pending")
        scripts = make_scripts_db(pending_row)
        generate_mock = self._patch_common(monkeypatch, scripts)

        await WorkflowService().generate_script_if_needed(
            workflow=make_workflow([BlockType.GOTO_URL, BlockType.CODE]),
            workflow_run=make_workflow_run(),
        )

        assert generate_mock.await_args.kwargs["script"].script_id == "s_pending"
        assert scripts.create_script.await_args.kwargs["script_id"] == "s_pending"

    @pytest.mark.asyncio
    async def test_finalize_writes_to_a_fresh_revision_not_the_pending_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """script_files is unique on (script_revision_id, file_path) and create_script_file
        conflict-noops, so regenerating into the pending revision would keep the stale
        pending main.py — which the in-flight per-block mint can still overwrite."""
        pending_row = SimpleNamespace(workflow_script_id="ws_1", script_id="s_pending")
        scripts = make_scripts_db(pending_row)
        generate_mock = self._patch_common(monkeypatch, scripts)

        await WorkflowService().generate_script_if_needed(
            workflow=make_workflow([BlockType.GOTO_URL, BlockType.CODE]),
            workflow_run=make_workflow_run(),
        )

        minted = generate_mock.await_args.kwargs["script"]
        assert minted.script_revision_id != "sr_pending"
        assert scripts.create_script.await_args.kwargs["version"] == 4

    @pytest.mark.asyncio
    async def test_finalize_does_not_pass_pending_revision_as_cached_source(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pending_row = SimpleNamespace(workflow_script_id="ws_1", script_id="s_pending")
        scripts = make_scripts_db(pending_row)
        generate_mock = self._patch_common(monkeypatch, scripts)

        await WorkflowService().generate_script_if_needed(
            workflow=make_workflow([BlockType.GOTO_URL, BlockType.CODE]),
            workflow_run=make_workflow_run(),
        )

        cached = generate_mock.await_args.kwargs["cached_script"]
        assert cached is not None and cached.script_revision_id == "sr_pending"

    @pytest.mark.asyncio
    async def test_first_run_finalize_creates_script_when_no_pending_exists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scripts = make_scripts_db(pending_row=None)
        generate_mock = self._patch_common(monkeypatch, scripts)

        await WorkflowService().generate_script_if_needed(
            workflow=make_workflow([BlockType.GOTO_URL, BlockType.CODE]),
            workflow_run=make_workflow_run(),
        )

        scripts.create_script.assert_awaited_once()
        assert generate_mock.await_args.kwargs["script"].script_id == "s_new"


class TestNoScriptFromATaskV3Run:
    """Task V3 actions carry no element data, so a script minted from them has dead selectors, and the
    workflow's later code-mode runs would load it. Both mint paths must refuse such a run."""

    @pytest.mark.parametrize(
        ("engines", "mints"),
        [
            ((RunEngine.skyvern_v1, RunEngine.skyvern_v1), True),
            ((RunEngine.skyvern_v1, RunEngine.skyvern_v3), False),
            ((RunEngine.skyvern_v3,), False),
        ],
    )
    @pytest.mark.asyncio
    async def test_finalize_mints_only_from_a_run_with_no_v3_block(
        self, monkeypatch: pytest.MonkeyPatch, engines: tuple[RunEngine, ...], mints: bool
    ) -> None:
        scripts = make_scripts_db(pending_row=None)
        generate_mock = TestFinalizeReusesPendingScript()._patch_common(monkeypatch, scripts, *engines)

        await WorkflowService().generate_script_if_needed(
            workflow=make_workflow([BlockType.GOTO_URL, BlockType.TASK]),
            workflow_run=make_workflow_run(),
            finalize=True,
        )

        assert scripts.create_script.await_count == int(mints)
        assert generate_mock.await_count == int(mints)

    @pytest.mark.parametrize(("engine", "mints"), [(RunEngine.skyvern_v1, True), (RunEngine.skyvern_v3, False)])
    @pytest.mark.asyncio
    async def test_pending_mint_skips_a_run_with_a_v3_block(
        self, monkeypatch: pytest.MonkeyPatch, engine: RunEngine, mints: bool
    ) -> None:
        monkeypatch.setattr(app, "DATABASE", SimpleNamespace(observer=run_blocks_db(engine)), raising=False)
        pending_mint = AsyncMock()
        monkeypatch.setattr(workflow_script_service, "generate_or_update_pending_workflow_script", pending_mint)

        await WorkflowService()._do_generate_pending_script(make_workflow([BlockType.TASK]), make_workflow_run())

        assert pending_mint.await_count == int(mints)

    @pytest.mark.parametrize(("kill_switch_on", "mints"), [(True, True), (False, False)])
    @pytest.mark.asyncio
    async def test_a_v3_pinned_block_the_kill_switch_ran_on_v1_still_gets_its_requested_script(
        self, monkeypatch: pytest.MonkeyPatch, kill_switch_on: bool, mints: bool
    ) -> None:
        workflow_run = make_workflow_run()
        workflow_run.code_gen = True
        # execute_safe labels the row with the resolved engine before dispatch.
        rows = BlockRowsByTask(**{"task-123": RunEngine.skyvern_v3})
        provider = MagicMock(spec=BaseExperimentationProvider)
        provider.resolve_feature_flag_strict = AsyncMock(return_value=kill_switch_on)

        await _run_execute_step_gate(
            engine=RunEngine.skyvern_v3,
            task_block=_make_block(TaskBlock, label="pinned", engine=RunEngine.skyvern_v3),
            experimentation_provider=provider,
            observer=rows,
            workflow_run_id=workflow_run.workflow_run_id,
        )
        scripts = make_scripts_db(pending_row=None)
        generate_mock = TestFinalizeReusesPendingScript()._patch_common(monkeypatch, scripts, observer=rows)

        await WorkflowService().generate_script_if_needed(
            workflow=make_workflow([BlockType.GOTO_URL, BlockType.TASK]),
            workflow_run=workflow_run,
        )

        assert generate_mock.await_count == int(mints)

    @pytest.mark.asyncio
    async def test_a_non_v3_dispatch_keeps_its_row_and_engine(self) -> None:
        rows = BlockRowsByTask(**{"task-123": RunEngine.openai_cua})

        _v3_mock, step_engine_mock = await _run_execute_step_gate(
            engine=RunEngine.openai_cua,
            task_block=_make_block(TaskBlock, label="cua", engine=RunEngine.openai_cua),
            observer=rows,
            workflow_run_id=make_workflow_run().workflow_run_id,
        )

        assert rows.engines == {"task-123": RunEngine.openai_cua}
        assert step_engine_mock.await_args.kwargs["engine"] == RunEngine.openai_cua

    @pytest.mark.parametrize(
        ("engine_at_block_read", "engine_after_action_read", "engine_read_fails", "mints"),
        [
            (None, RunEngine.skyvern_v1, False, True),
            # The cached-script fallback stamps the row after the block read, as its v3 actions land.
            (None, RunEngine.skyvern_v3, False, False),
            # A retry moves the v3 row to a new task and the kill switch relabels it v1 before the re-read.
            (RunEngine.skyvern_v3, RunEngine.skyvern_v1, False, False),
            (None, RunEngine.skyvern_v1, True, False),
        ],
        ids=["stays_v1", "stamped_v3_after_block_read", "relabeled_v1_after_block_read", "engine_read_fails_closed"],
    )
    @pytest.mark.asyncio
    async def test_an_in_flight_pending_mint_never_writes_a_v3_tasks_actions(
        self,
        monkeypatch: pytest.MonkeyPatch,
        engine_at_block_read: RunEngine | None,
        engine_after_action_read: RunEngine,
        engine_read_fails: bool,
        mints: bool,
    ) -> None:
        # The mint passed its v3 check before the second block ran; the row changes between its two reads.
        def run_block(label: str, task_id: str, engine: RunEngine | None, created_at: int) -> SimpleNamespace:
            return SimpleNamespace(
                workflow_run_block_id=f"wrb_{label}",
                parent_workflow_run_block_id=None,
                block_type=BlockType.TASK,
                label=label,
                task_id=task_id,
                status="completed",
                output={},
                created_at=created_at,
                engine=engine,
            )

        definition_blocks = [
            SimpleNamespace(label=label, block_type=BlockType.TASK, model_dump=lambda label=label: {"label": label})
            for label in ("cached", "pinned")
        ]
        workflow = SimpleNamespace(
            workflow_id="w_1",
            workflow_permanent_id="wpid_1",
            organization_id="o_1",
            title="wf",
            workflow_definition=SimpleNamespace(blocks=definition_blocks),
            model_dump=lambda: {"workflow_id": "w_1"},
        )
        run_request = SimpleNamespace(workflow_id="wpid_1", model_dump=lambda: {"workflow_id": "wpid_1"})
        tasks = [
            SimpleNamespace(task_id=t, model_dump=lambda t=t: {"task_id": t}) for t in ("tsk_cached", "tsk_pinned")
        ]
        actions = [
            ClickAction(action_id=f"a_{t}", task_id=t, element_id=f"el_{t}") for t in ("tsk_pinned", "tsk_cached")
        ]
        rows = [
            run_block("cached", "tsk_cached", RunEngine.skyvern_v1, 1),
            run_block("pinned", "tsk_pinned", engine_at_block_read, 2),
        ]

        async def read_blocks(**_: Any) -> list[SimpleNamespace]:
            return [SimpleNamespace(**vars(row)) for row in rows]

        async def read_actions(**_: Any) -> list[ClickAction]:
            rows[1].engine = engine_after_action_read
            return list(actions)

        async def has_block_on_engine(*, workflow_run_id: str, engine: RunEngine, organization_id: str) -> bool:
            if engine_read_fails:
                raise RuntimeError("database unavailable")
            return any(row.engine == engine for row in rows)

        database = SimpleNamespace(
            observer=SimpleNamespace(
                get_workflow_run_blocks=read_blocks, workflow_run_has_block_on_engine=has_block_on_engine
            ),
            tasks=SimpleNamespace(get_tasks_by_ids=AsyncMock(return_value=tasks), get_tasks_actions=read_actions),
        )
        monkeypatch.setattr(app, "DATABASE", database, raising=False)
        monkeypatch.setattr(
            app,
            "WORKFLOW_SERVICE",
            SimpleNamespace(get_workflow_by_permanent_id=AsyncMock(return_value=workflow)),
            raising=False,
        )
        monkeypatch.setattr(
            workflow_service_module,
            "get_workflow_run_response",
            AsyncMock(return_value=SimpleNamespace(run_request=run_request)),
        )
        monkeypatch.setattr(workflow_script_service, "is_adaptive_caching", lambda *_: False)
        codegen = AsyncMock(side_effect=RuntimeError("stop after the generation input is accepted"))
        monkeypatch.setattr(workflow_script_service, "generate_workflow_script_python_code", codegen)

        await workflow_script_service.generate_workflow_script(
            workflow_run=make_workflow_run(),
            workflow=workflow,
            script=SimpleNamespace(script_id="s_1", script_revision_id="sr_1", version=1),
            rendered_cache_key_value="default:site",
            pending=True,
        )

        assert codegen.await_count == int(mints)
