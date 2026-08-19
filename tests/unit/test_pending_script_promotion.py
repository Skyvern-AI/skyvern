"""
Tests for pending-script promotion and per-block mint convergence (SKY-13659).

Defect A: the end-of-run finalize looked up only published scripts, missed the
pending script the same run minted per-block, and created a duplicate script row
with identical content.

Defect B: the per-block pending-mint path counted non-cacheable blocks (goto/code)
as "missing" forever, so code/goto-only workflows regenerated the full script after
every block instead of once.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.schemas.scripts import ScriptStatus
from skyvern.schemas.workflows import BlockStatus, BlockType
from skyvern.services import workflow_script_service


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


class TestFinalizeReusesPendingScript:
    def _patch_common(self, monkeypatch: pytest.MonkeyPatch, scripts: MagicMock) -> AsyncMock:
        monkeypatch.setattr(app, "DATABASE", SimpleNamespace(scripts=scripts), raising=False)
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
