"""Unit tests for artifact bundling in the cached script execution path.

Tests that ScriptSkyvernPage artifact methods route to accumulate_* when
use_artifact_bundling is True, and to create_artifact() when False.
Also tests the flush in _update_workflow_block().
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.core.script_generations.script_skyvern_page import ScriptSkyvernPage
from skyvern.forge.sdk.artifact.manager import ArtifactManager
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.artifact.storage.test_helpers import TEST_ORGANIZATION_ID, TEST_TASK_ID, create_fake_step
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.services.script_service import _update_workflow_block

TEST_STEP_ID = "step_cached_bundle_001"
TEST_WORKFLOW_RUN_ID = "wr_test_cached_001"
TEST_WORKFLOW_RUN_BLOCK_ID = "wrb_test_cached_001"
TEST_RUN_ID = "run_test_cached_001"


# ---------------------------------------------------------------------------
# accumulate_screenshot_to_step_archive: SCREENSHOT_FINAL prefix
# ---------------------------------------------------------------------------


class TestScreenshotFinalPrefix:
    """SCREENSHOT_FINAL should get its own prefix in the step archive."""

    def test_final_screenshots_use_screenshot_final_prefix(self) -> None:
        manager = ArtifactManager()
        step = create_fake_step(TEST_STEP_ID)
        ids = manager.accumulate_screenshot_to_step_archive(
            step=step, screenshots=[b"png_final"], artifact_type=ArtifactType.SCREENSHOT_FINAL
        )
        acc = manager._step_archives[step.step_id]
        assert "screenshot_final_0.png" in acc.entries
        assert acc.entries["screenshot_final_0.png"] == b"png_final"
        assert len(ids) == 1

    def test_final_and_action_screenshots_have_separate_indices(self) -> None:
        """SCREENSHOT_FINAL and SCREENSHOT_ACTION should not share index counters."""
        manager = ArtifactManager()
        step = create_fake_step(TEST_STEP_ID)
        manager.accumulate_screenshot_to_step_archive(
            step=step, screenshots=[b"action_png"], artifact_type=ArtifactType.SCREENSHOT_ACTION
        )
        manager.accumulate_screenshot_to_step_archive(
            step=step, screenshots=[b"final_png"], artifact_type=ArtifactType.SCREENSHOT_FINAL
        )
        acc = manager._step_archives[step.step_id]
        assert "screenshot_action_0.png" in acc.entries
        assert "screenshot_final_0.png" in acc.entries
        assert len(acc.entries) == 2


# ---------------------------------------------------------------------------
# ScriptSkyvernPage bundling branch tests
# ---------------------------------------------------------------------------


TEST_WORKFLOW_ID = "wf_test_cached_001"


def _make_context(use_bundling: bool) -> SkyvernContext:
    return SkyvernContext(
        organization_id=TEST_ORGANIZATION_ID,
        task_id=TEST_TASK_ID,
        step_id=TEST_STEP_ID,
        workflow_id=TEST_WORKFLOW_ID,
        workflow_run_id=TEST_WORKFLOW_RUN_ID,
        workflow_run_block_id=TEST_WORKFLOW_RUN_BLOCK_ID,
        run_id=TEST_RUN_ID,
        use_artifact_bundling=use_bundling,
    )


class TestScreenshotAfterExecutionBundling:
    """_create_screenshot_after_execution routes to accumulate or create_artifact."""

    @pytest.mark.asyncio
    async def test_bundling_enabled_uses_accumulate(self) -> None:
        context = _make_context(use_bundling=True)
        step = create_fake_step(TEST_STEP_ID)
        mock_browser_state = MagicMock()
        mock_browser_state.take_post_action_screenshot = AsyncMock(return_value=b"screenshot_data")

        mock_manager = MagicMock(spec=ArtifactManager)
        mock_manager.accumulate_screenshot_to_step_archive = MagicMock(return_value=["aid_1"])
        mock_manager.create_artifact = AsyncMock()

        with (
            patch("skyvern.core.script_generations.script_skyvern_page.skyvern_context") as mock_ctx,
            patch("skyvern.core.script_generations.script_skyvern_page.app") as mock_app,
            patch.object(ScriptSkyvernPage, "_get_browser_state", new_callable=AsyncMock) as mock_get_bs,
        ):
            mock_ctx.ensure_context.return_value = context
            mock_get_bs.return_value = mock_browser_state
            mock_app.DATABASE.get_step = AsyncMock(return_value=step)
            mock_app.ARTIFACT_MANAGER = mock_manager

            await ScriptSkyvernPage._create_screenshot_after_execution()

            mock_manager.accumulate_screenshot_to_step_archive.assert_called_once_with(
                step=step,
                screenshots=[b"screenshot_data"],
                artifact_type=ArtifactType.SCREENSHOT_ACTION,
                workflow_run_id=TEST_WORKFLOW_RUN_ID,
                workflow_run_block_id=TEST_WORKFLOW_RUN_BLOCK_ID,
                run_id=TEST_RUN_ID,
            )
            mock_manager.create_artifact.assert_not_called()

    @pytest.mark.asyncio
    async def test_bundling_disabled_uses_create_artifact(self) -> None:
        context = _make_context(use_bundling=False)
        step = create_fake_step(TEST_STEP_ID)
        mock_browser_state = MagicMock()
        mock_browser_state.take_post_action_screenshot = AsyncMock(return_value=b"screenshot_data")

        mock_manager = MagicMock(spec=ArtifactManager)
        mock_manager.accumulate_screenshot_to_step_archive = MagicMock()
        mock_manager.create_artifact = AsyncMock(return_value="aid_1")

        with (
            patch("skyvern.core.script_generations.script_skyvern_page.skyvern_context") as mock_ctx,
            patch("skyvern.core.script_generations.script_skyvern_page.app") as mock_app,
            patch.object(ScriptSkyvernPage, "_get_browser_state", new_callable=AsyncMock) as mock_get_bs,
        ):
            mock_ctx.ensure_context.return_value = context
            mock_get_bs.return_value = mock_browser_state
            mock_app.DATABASE.get_step = AsyncMock(return_value=step)
            mock_app.ARTIFACT_MANAGER = mock_manager

            await ScriptSkyvernPage._create_screenshot_after_execution()

            mock_manager.create_artifact.assert_called_once_with(
                step=step,
                artifact_type=ArtifactType.SCREENSHOT_ACTION,
                data=b"screenshot_data",
            )
            mock_manager.accumulate_screenshot_to_step_archive.assert_not_called()


class TestHtmlActionAfterExecutionBundling:
    """_create_html_action_after_execution routes to accumulate or create_artifact."""

    @pytest.mark.asyncio
    async def test_bundling_enabled_uses_accumulate(self) -> None:
        context = _make_context(use_bundling=True)
        step = create_fake_step(TEST_STEP_ID)
        html_content = "<html><body>test</body></html>"

        mock_browser_state = MagicMock()
        mock_working_page = AsyncMock()
        mock_browser_state.get_working_page = AsyncMock(return_value=mock_working_page)

        mock_manager = MagicMock(spec=ArtifactManager)
        mock_manager.accumulate_action_html_to_archive = MagicMock()
        mock_manager.create_artifact = AsyncMock()

        with (
            patch("skyvern.core.script_generations.script_skyvern_page.skyvern_context") as mock_ctx,
            patch("skyvern.core.script_generations.script_skyvern_page.app") as mock_app,
            patch.object(ScriptSkyvernPage, "_get_browser_state", new_callable=AsyncMock) as mock_get_bs,
            patch("skyvern.core.script_generations.script_skyvern_page.SkyvernFrame") as mock_frame_cls,
        ):
            mock_ctx.ensure_context.return_value = context
            mock_get_bs.return_value = mock_browser_state
            mock_app.DATABASE.get_step = AsyncMock(return_value=step)
            mock_app.ARTIFACT_MANAGER = mock_manager

            mock_frame = MagicMock()
            mock_frame.get_content = AsyncMock(return_value=html_content)
            mock_frame_cls.create_instance = AsyncMock(return_value=mock_frame)

            await ScriptSkyvernPage._create_html_action_after_execution()

            mock_manager.accumulate_action_html_to_archive.assert_called_once_with(
                step=step,
                html_action=html_content.encode("utf-8"),
                workflow_run_id=TEST_WORKFLOW_RUN_ID,
                workflow_run_block_id=TEST_WORKFLOW_RUN_BLOCK_ID,
                run_id=TEST_RUN_ID,
            )
            mock_manager.create_artifact.assert_not_called()

    @pytest.mark.asyncio
    async def test_bundling_disabled_uses_create_artifact(self) -> None:
        context = _make_context(use_bundling=False)
        step = create_fake_step(TEST_STEP_ID)
        html_content = "<html><body>test</body></html>"

        mock_browser_state = MagicMock()
        mock_working_page = AsyncMock()
        mock_browser_state.get_working_page = AsyncMock(return_value=mock_working_page)

        mock_manager = MagicMock(spec=ArtifactManager)
        mock_manager.accumulate_action_html_to_archive = MagicMock()
        mock_manager.create_artifact = AsyncMock(return_value="aid_1")

        with (
            patch("skyvern.core.script_generations.script_skyvern_page.skyvern_context") as mock_ctx,
            patch("skyvern.core.script_generations.script_skyvern_page.app") as mock_app,
            patch.object(ScriptSkyvernPage, "_get_browser_state", new_callable=AsyncMock) as mock_get_bs,
            patch("skyvern.core.script_generations.script_skyvern_page.SkyvernFrame") as mock_frame_cls,
        ):
            mock_ctx.ensure_context.return_value = context
            mock_get_bs.return_value = mock_browser_state
            mock_app.DATABASE.get_step = AsyncMock(return_value=step)
            mock_app.ARTIFACT_MANAGER = mock_manager

            mock_frame = MagicMock()
            mock_frame.get_content = AsyncMock(return_value=html_content)
            mock_frame_cls.create_instance = AsyncMock(return_value=mock_frame)

            await ScriptSkyvernPage._create_html_action_after_execution()

            mock_manager.create_artifact.assert_called_once_with(
                step=step,
                artifact_type=ArtifactType.HTML_ACTION,
                data=html_content.encode("utf-8"),
            )
            mock_manager.accumulate_action_html_to_archive.assert_not_called()


class TestFinalScreenshotBundling:
    """_create_final_screenshot routes to accumulate or create_artifact."""

    @pytest.mark.asyncio
    async def test_bundling_enabled_uses_accumulate(self) -> None:
        context = _make_context(use_bundling=True)
        step = create_fake_step(TEST_STEP_ID)
        mock_browser_state = MagicMock()
        mock_browser_state.get_working_page = AsyncMock(return_value=MagicMock())
        mock_browser_state.take_fullpage_screenshot = AsyncMock(return_value=b"fullpage_png")

        mock_manager = MagicMock(spec=ArtifactManager)
        mock_manager.accumulate_screenshot_to_step_archive = MagicMock(return_value=["aid_final"])
        mock_manager.create_artifact = AsyncMock()

        with (
            patch("skyvern.core.script_generations.script_skyvern_page.skyvern_context") as mock_ctx,
            patch("skyvern.core.script_generations.script_skyvern_page.app") as mock_app,
            patch.object(ScriptSkyvernPage, "_get_browser_state", new_callable=AsyncMock) as mock_get_bs,
        ):
            mock_ctx.ensure_context.return_value = context
            mock_get_bs.return_value = mock_browser_state
            mock_app.DATABASE.get_step = AsyncMock(return_value=step)
            mock_app.ARTIFACT_MANAGER = mock_manager

            await ScriptSkyvernPage._create_final_screenshot()

            mock_manager.accumulate_screenshot_to_step_archive.assert_called_once_with(
                step=step,
                screenshots=[b"fullpage_png"],
                artifact_type=ArtifactType.SCREENSHOT_FINAL,
                workflow_run_id=TEST_WORKFLOW_RUN_ID,
                workflow_run_block_id=TEST_WORKFLOW_RUN_BLOCK_ID,
                run_id=TEST_RUN_ID,
            )
            mock_manager.create_artifact.assert_not_called()

    @pytest.mark.asyncio
    async def test_bundling_disabled_uses_create_artifact(self) -> None:
        context = _make_context(use_bundling=False)
        step = create_fake_step(TEST_STEP_ID)
        mock_browser_state = MagicMock()
        mock_browser_state.get_working_page = AsyncMock(return_value=MagicMock())
        mock_browser_state.take_fullpage_screenshot = AsyncMock(return_value=b"fullpage_png")

        mock_manager = MagicMock(spec=ArtifactManager)
        mock_manager.accumulate_screenshot_to_step_archive = MagicMock()
        mock_manager.create_artifact = AsyncMock(return_value="aid_final")

        with (
            patch("skyvern.core.script_generations.script_skyvern_page.skyvern_context") as mock_ctx,
            patch("skyvern.core.script_generations.script_skyvern_page.app") as mock_app,
            patch.object(ScriptSkyvernPage, "_get_browser_state", new_callable=AsyncMock) as mock_get_bs,
        ):
            mock_ctx.ensure_context.return_value = context
            mock_get_bs.return_value = mock_browser_state
            mock_app.DATABASE.get_step = AsyncMock(return_value=step)
            mock_app.ARTIFACT_MANAGER = mock_manager

            await ScriptSkyvernPage._create_final_screenshot()

            mock_manager.create_artifact.assert_called_once_with(
                step=step,
                artifact_type=ArtifactType.SCREENSHOT_FINAL,
                data=b"fullpage_png",
            )
            mock_manager.accumulate_screenshot_to_step_archive.assert_not_called()


# ---------------------------------------------------------------------------
# _update_workflow_block flush tests
# ---------------------------------------------------------------------------


class TestUpdateWorkflowBlockFlush:
    """_update_workflow_block flushes step archive when bundling is enabled."""

    @pytest.mark.asyncio
    async def test_flush_called_when_bundling_enabled(self) -> None:
        context = _make_context(use_bundling=True)
        mock_manager = MagicMock(spec=ArtifactManager)
        mock_manager.flush_step_archive = AsyncMock()

        with (
            patch("skyvern.services.script_service.skyvern_context") as mock_ctx,
            patch("skyvern.services.script_service.app") as mock_app,
            patch("skyvern.services.script_service.script_run_context_manager") as mock_run_ctx,
        ):
            mock_ctx.current.return_value = context
            mock_app.ARTIFACT_MANAGER = mock_manager
            mock_app.DATABASE.update_step = AsyncMock()
            mock_app.DATABASE.update_task = AsyncMock(return_value=MagicMock(extracted_information=None))
            mock_app.DATABASE.update_workflow_run_block = AsyncMock()
            mock_app.STORAGE.get_downloaded_files = AsyncMock(return_value=[])
            mock_app.WORKFLOW_SERVICE.get_recent_task_screenshot_artifacts = AsyncMock(return_value=[])
            mock_app.WORKFLOW_SERVICE.get_recent_workflow_screenshot_artifacts = AsyncMock(return_value=[])
            mock_app.WORKFLOW_SERVICE.send_workflow_response = AsyncMock()
            mock_run_ctx.get_run_context.return_value = None

            await _update_workflow_block(
                workflow_run_block_id=TEST_WORKFLOW_RUN_BLOCK_ID,
                status=MagicMock(value="completed"),
                task_id=TEST_TASK_ID,
                step_id=TEST_STEP_ID,
            )

            mock_manager.flush_step_archive.assert_awaited_once_with(TEST_STEP_ID)

    @pytest.mark.asyncio
    async def test_flush_not_called_when_bundling_disabled(self) -> None:
        context = _make_context(use_bundling=False)
        mock_manager = MagicMock(spec=ArtifactManager)
        mock_manager.flush_step_archive = AsyncMock()

        with (
            patch("skyvern.services.script_service.skyvern_context") as mock_ctx,
            patch("skyvern.services.script_service.app") as mock_app,
            patch("skyvern.services.script_service.script_run_context_manager") as mock_run_ctx,
        ):
            mock_ctx.current.return_value = context
            mock_app.ARTIFACT_MANAGER = mock_manager
            mock_app.DATABASE.update_step = AsyncMock()
            mock_app.DATABASE.update_task = AsyncMock(return_value=MagicMock(extracted_information=None))
            mock_app.DATABASE.update_workflow_run_block = AsyncMock()
            mock_app.STORAGE.get_downloaded_files = AsyncMock(return_value=[])
            mock_app.WORKFLOW_SERVICE.get_recent_task_screenshot_artifacts = AsyncMock(return_value=[])
            mock_app.WORKFLOW_SERVICE.get_recent_workflow_screenshot_artifacts = AsyncMock(return_value=[])
            mock_app.WORKFLOW_SERVICE.send_workflow_response = AsyncMock()
            mock_run_ctx.get_run_context.return_value = None

            await _update_workflow_block(
                workflow_run_block_id=TEST_WORKFLOW_RUN_BLOCK_ID,
                status=MagicMock(value="completed"),
                task_id=TEST_TASK_ID,
                step_id=TEST_STEP_ID,
            )

            mock_manager.flush_step_archive.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_flush_not_called_without_step_id(self) -> None:
        context = _make_context(use_bundling=True)
        mock_manager = MagicMock(spec=ArtifactManager)
        mock_manager.flush_step_archive = AsyncMock()

        with (
            patch("skyvern.services.script_service.skyvern_context") as mock_ctx,
            patch("skyvern.services.script_service.app") as mock_app,
            patch("skyvern.services.script_service.script_run_context_manager") as mock_run_ctx,
        ):
            mock_ctx.current.return_value = context
            mock_app.ARTIFACT_MANAGER = mock_manager
            mock_app.DATABASE.update_workflow_run_block = AsyncMock()
            mock_app.WORKFLOW_SERVICE.send_workflow_response = AsyncMock()
            mock_run_ctx.get_run_context.return_value = None

            await _update_workflow_block(
                workflow_run_block_id=TEST_WORKFLOW_RUN_BLOCK_ID,
                status=MagicMock(value="completed"),
                task_id=None,
                step_id=None,
            )

            mock_manager.flush_step_archive.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_flush_failure_does_not_block_step_finalization(self) -> None:
        """If flush_step_archive raises, _update_workflow_block should still proceed."""

        context = _make_context(use_bundling=True)
        mock_manager = MagicMock(spec=ArtifactManager)
        mock_manager.flush_step_archive = AsyncMock(side_effect=RuntimeError("S3 timeout"))

        with (
            patch("skyvern.services.script_service.skyvern_context") as mock_ctx,
            patch("skyvern.services.script_service.app") as mock_app,
            patch("skyvern.services.script_service.script_run_context_manager") as mock_run_ctx,
        ):
            mock_ctx.current.return_value = context
            mock_app.ARTIFACT_MANAGER = mock_manager
            mock_app.DATABASE.update_step = AsyncMock()
            mock_app.DATABASE.update_task = AsyncMock(return_value=MagicMock(extracted_information=None))
            mock_app.DATABASE.update_workflow_run_block = AsyncMock()
            mock_app.STORAGE.get_downloaded_files = AsyncMock(return_value=[])
            mock_app.WORKFLOW_SERVICE.get_recent_task_screenshot_artifacts = AsyncMock(return_value=[])
            mock_app.WORKFLOW_SERVICE.get_recent_workflow_screenshot_artifacts = AsyncMock(return_value=[])
            mock_app.WORKFLOW_SERVICE.send_workflow_response = AsyncMock()
            mock_run_ctx.get_run_context.return_value = None

            # Should NOT raise despite flush failure
            await _update_workflow_block(
                workflow_run_block_id=TEST_WORKFLOW_RUN_BLOCK_ID,
                status=MagicMock(value="completed"),
                task_id=TEST_TASK_ID,
                step_id=TEST_STEP_ID,
            )

            # Flush was attempted
            mock_manager.flush_step_archive.assert_awaited_once_with(TEST_STEP_ID)


# ---------------------------------------------------------------------------
# workflow_run_block_id context propagation
# ---------------------------------------------------------------------------


class TestWorkflowRunBlockIdContext:
    """SkyvernContext.workflow_run_block_id field exists and defaults correctly."""

    def test_default_is_none(self) -> None:
        ctx = SkyvernContext()
        assert ctx.workflow_run_block_id is None

    def test_can_be_set(self) -> None:
        ctx = SkyvernContext(workflow_run_block_id="wrb_123")
        assert ctx.workflow_run_block_id == "wrb_123"

    def test_set_after_creation(self) -> None:
        ctx = SkyvernContext()
        ctx.workflow_run_block_id = "wrb_456"
        assert ctx.workflow_run_block_id == "wrb_456"
