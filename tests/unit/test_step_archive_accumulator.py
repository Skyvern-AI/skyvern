"""Unit tests for the step/task archive accumulation logic in ArtifactManager.

These tests exercise the in-memory accumulation helpers and the ZIP-building
utility without touching S3 or the database.
"""

import io
import zipfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.artifact.manager import ArtifactManager, StepArchiveAccumulator
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.artifact.storage.test_helpers import create_fake_step

TEST_STEP_ID = "step_archive_test_001"
TEST_STEP_ID_2 = "step_archive_test_002"


# ---------------------------------------------------------------------------
# StepArchiveAccumulator helpers
# ---------------------------------------------------------------------------


class TestAddToStepArchive:
    """Tests for ArtifactManager._add_to_step_archive."""

    def _make_acc(self, step_id: str = TEST_STEP_ID) -> StepArchiveAccumulator:
        step = create_fake_step(step_id)
        return StepArchiveAccumulator(
            step=step,
            workflow_run_id=None,
            workflow_run_block_id=None,
            run_id=None,
        )

    def test_add_single_entry_returns_stable_artifact_id(self) -> None:
        manager = ArtifactManager()
        acc = self._make_acc()
        aid = manager._add_to_step_archive(acc, "scrape.html", b"<html/>", ArtifactType.HTML_SCRAPE)
        assert isinstance(aid, str)
        assert len(aid) > 0
        assert acc.entries["scrape.html"] == b"<html/>"
        assert acc.member_types[0] == (ArtifactType.HTML_SCRAPE, "scrape.html", aid)

    def test_add_with_explicit_artifact_id(self) -> None:
        manager = ArtifactManager()
        acc = self._make_acc()
        aid = manager._add_to_step_archive(
            acc, "scrape.html", b"<html/>", ArtifactType.HTML_SCRAPE, artifact_id="custom_id_abc"
        )
        assert aid == "custom_id_abc"
        assert acc.member_types[0][2] == "custom_id_abc"

    def test_deduplication_preserves_existing_artifact_id(self) -> None:
        """Adding the same filename twice should update bytes but keep the original artifact_id."""
        manager = ArtifactManager()
        acc = self._make_acc()
        aid_first = manager._add_to_step_archive(acc, "scrape.html", b"v1", ArtifactType.HTML_SCRAPE)
        aid_second = manager._add_to_step_archive(acc, "scrape.html", b"v2", ArtifactType.HTML_SCRAPE)
        assert aid_first == aid_second
        assert acc.entries["scrape.html"] == b"v2"
        # Only one member_types entry for that filename
        entries_for_filename = [m for m in acc.member_types if m[1] == "scrape.html"]
        assert len(entries_for_filename) == 1

    def test_multiple_distinct_entries(self) -> None:
        manager = ArtifactManager()
        acc = self._make_acc()
        manager._add_to_step_archive(acc, "scrape.html", b"html", ArtifactType.HTML_SCRAPE)
        manager._add_to_step_archive(acc, "element_tree.json", b"{}", ArtifactType.VISIBLE_ELEMENTS_TREE)
        assert len(acc.entries) == 2
        assert len(acc.member_types) == 2


class TestAccumulateScrapeToArchive:
    """Tests for ArtifactManager.accumulate_scrape_to_archive."""

    def test_adds_six_entries(self) -> None:
        manager = ArtifactManager()
        step = create_fake_step(TEST_STEP_ID)
        manager.accumulate_scrape_to_archive(
            step=step,
            html=b"<html/>",
            id_css_map=b'{"a": "b"}',
            id_frame_map=b'{"c": "d"}',
            element_tree=b"[]",
            element_tree_trimmed=b"[]",
            element_tree_in_prompt=b"prompt text",
        )
        acc = manager._step_archives[step.step_id]
        assert len(acc.entries) == 6
        assert acc.entries["scrape.html"] == b"<html/>"
        assert acc.entries["id_css_map.json"] == b'{"a": "b"}'
        assert acc.entries["element_tree_in_prompt.txt"] == b"prompt text"

    def test_member_types_has_correct_artifact_types(self) -> None:
        manager = ArtifactManager()
        step = create_fake_step(TEST_STEP_ID)
        manager.accumulate_scrape_to_archive(
            step=step,
            html=b"<html/>",
            id_css_map=b"{}",
            id_frame_map=b"{}",
            element_tree=b"[]",
            element_tree_trimmed=b"[]",
            element_tree_in_prompt=b"",
        )
        acc = manager._step_archives[step.step_id]
        types = {m[0] for m in acc.member_types}
        assert ArtifactType.HTML_SCRAPE in types
        assert ArtifactType.VISIBLE_ELEMENTS_ID_CSS_MAP in types
        assert ArtifactType.VISIBLE_ELEMENTS_ID_FRAME_MAP in types
        assert ArtifactType.VISIBLE_ELEMENTS_TREE in types
        assert ArtifactType.VISIBLE_ELEMENTS_TREE_TRIMMED in types
        assert ArtifactType.VISIBLE_ELEMENTS_TREE_IN_PROMPT in types

    def test_idempotent_on_second_call(self) -> None:
        """Calling accumulate_scrape_to_archive twice should overwrite, not duplicate."""
        manager = ArtifactManager()
        step = create_fake_step(TEST_STEP_ID)
        manager.accumulate_scrape_to_archive(
            step=step,
            html=b"v1",
            id_css_map=b"v1",
            id_frame_map=b"v1",
            element_tree=b"v1",
            element_tree_trimmed=b"v1",
            element_tree_in_prompt=b"v1",
        )
        manager.accumulate_scrape_to_archive(
            step=step,
            html=b"v2",
            id_css_map=b"v2",
            id_frame_map=b"v2",
            element_tree=b"v2",
            element_tree_trimmed=b"v2",
            element_tree_in_prompt=b"v2",
        )
        acc = manager._step_archives[step.step_id]
        assert acc.entries["scrape.html"] == b"v2"
        # member_types should not have duplicates for each filename
        filenames = [m[1] for m in acc.member_types]
        assert len(filenames) == len(set(filenames))


class TestAccumulateLlmCallToArchive:
    """Tests for ArtifactManager.accumulate_llm_call_to_archive."""

    def test_adds_all_provided_artifacts(self) -> None:
        manager = ArtifactManager()
        step = create_fake_step(TEST_STEP_ID)
        manager.accumulate_llm_call_to_archive(
            step=step,
            hashed_href_map=b'{"href": "url"}',
            prompt=b"you are a bot",
            request=b'{"model": "x"}',
            response=b'{"choices": []}',
            parsed_response=b'{"actions": []}',
            rendered_response=b'{"actions": []}',
        )
        acc = manager._step_archives[step.step_id]
        assert "hashed_href_map_0.json" in acc.entries
        assert "llm_prompt_0.txt" in acc.entries
        assert "llm_request_0.json" in acc.entries
        assert "llm_response_0.json" in acc.entries
        assert "llm_response_parsed_0.json" in acc.entries
        assert "llm_response_rendered_0.json" in acc.entries

    def test_none_values_not_added(self) -> None:
        manager = ArtifactManager()
        step = create_fake_step(TEST_STEP_ID)
        manager.accumulate_llm_call_to_archive(
            step=step,
            prompt=b"only prompt",
            response=None,
            parsed_response=None,
        )
        acc = manager._step_archives[step.step_id]
        assert "llm_prompt_0.txt" in acc.entries
        assert "llm_response_0.json" not in acc.entries
        assert "llm_response_parsed_0.json" not in acc.entries

    def test_multiple_llm_calls_use_distinct_indexed_filenames(self) -> None:
        """Each LLM call within the same step gets its own index — no data overwritten."""
        manager = ArtifactManager()
        step = create_fake_step(TEST_STEP_ID)
        manager.accumulate_llm_call_to_archive(step=step, prompt=b"call 1")
        manager.accumulate_llm_call_to_archive(step=step, prompt=b"call 2")
        acc = manager._step_archives[step.step_id]
        assert acc.entries["llm_prompt_0.txt"] == b"call 1"
        assert acc.entries["llm_prompt_1.txt"] == b"call 2"
        assert acc.llm_call_count == 2
        filenames = [m[1] for m in acc.member_types]
        # Both prompts present — no silent overwrite
        assert "llm_prompt_0.txt" in filenames
        assert "llm_prompt_1.txt" in filenames

    def test_llm_call_count_increments(self) -> None:
        manager = ArtifactManager()
        step = create_fake_step(TEST_STEP_ID)
        assert manager._get_or_create_step_archive(step, None, None, None).llm_call_count == 0
        manager.accumulate_llm_call_to_archive(step=step, prompt=b"p1")
        assert manager._step_archives[step.step_id].llm_call_count == 1
        manager.accumulate_llm_call_to_archive(step=step, prompt=b"p2")
        assert manager._step_archives[step.step_id].llm_call_count == 2


class TestAccumulateActionHtmlToArchive:
    """Tests for ArtifactManager.accumulate_action_html_to_archive."""

    def test_first_action_gets_index_zero(self) -> None:
        manager = ArtifactManager()
        step = create_fake_step(TEST_STEP_ID)
        manager.accumulate_action_html_to_archive(step=step, html_action=b"<body>action0</body>")
        acc = manager._step_archives[step.step_id]
        assert "html_action_0.html" in acc.entries
        assert acc.entries["html_action_0.html"] == b"<body>action0</body>"

    def test_multiple_actions_get_sequential_indices(self) -> None:
        manager = ArtifactManager()
        step = create_fake_step(TEST_STEP_ID)
        for i in range(3):
            manager.accumulate_action_html_to_archive(step=step, html_action=f"<body>{i}</body>".encode())
        acc = manager._step_archives[step.step_id]
        assert "html_action_0.html" in acc.entries
        assert "html_action_1.html" in acc.entries
        assert "html_action_2.html" in acc.entries
        assert acc.entries["html_action_1.html"] == b"<body>1</body>"

    def test_member_types_have_html_action_type(self) -> None:
        manager = ArtifactManager()
        step = create_fake_step(TEST_STEP_ID)
        manager.accumulate_action_html_to_archive(step=step, html_action=b"<html/>")
        acc = manager._step_archives[step.step_id]
        assert any(m[0] == ArtifactType.HTML_ACTION for m in acc.member_types)


class TestAccumulateScreenshotToStepArchive:
    """Tests for ArtifactManager.accumulate_screenshot_to_step_archive."""

    def test_returns_artifact_ids(self) -> None:
        manager = ArtifactManager()
        step = create_fake_step(TEST_STEP_ID)
        ids = manager.accumulate_screenshot_to_step_archive(
            step=step,
            screenshots=[b"png1", b"png2"],
            artifact_type=ArtifactType.SCREENSHOT_LLM,
        )
        assert len(ids) == 2
        assert all(isinstance(i, str) and len(i) > 0 for i in ids)
        assert ids[0] != ids[1]

    def test_llm_screenshots_use_screenshot_llm_prefix(self) -> None:
        manager = ArtifactManager()
        step = create_fake_step(TEST_STEP_ID)
        manager.accumulate_screenshot_to_step_archive(
            step=step, screenshots=[b"png"], artifact_type=ArtifactType.SCREENSHOT_LLM
        )
        acc = manager._step_archives[step.step_id]
        assert "screenshot_llm_0.png" in acc.entries

    def test_action_screenshots_use_screenshot_action_prefix(self) -> None:
        manager = ArtifactManager()
        step = create_fake_step(TEST_STEP_ID)
        manager.accumulate_screenshot_to_step_archive(
            step=step, screenshots=[b"png"], artifact_type=ArtifactType.SCREENSHOT_ACTION
        )
        acc = manager._step_archives[step.step_id]
        assert "screenshot_action_0.png" in acc.entries

    def test_sequential_indices_across_calls(self) -> None:
        """Two separate accumulate calls should increment the index."""
        manager = ArtifactManager()
        step = create_fake_step(TEST_STEP_ID)
        manager.accumulate_screenshot_to_step_archive(
            step=step, screenshots=[b"a"], artifact_type=ArtifactType.SCREENSHOT_LLM
        )
        manager.accumulate_screenshot_to_step_archive(
            step=step, screenshots=[b"b"], artifact_type=ArtifactType.SCREENSHOT_LLM
        )
        acc = manager._step_archives[step.step_id]
        assert "screenshot_llm_0.png" in acc.entries
        assert "screenshot_llm_1.png" in acc.entries
        assert acc.entries["screenshot_llm_0.png"] == b"a"
        assert acc.entries["screenshot_llm_1.png"] == b"b"

    def test_artifact_ids_stable_across_calls(self) -> None:
        """The pre-generated ID for index 0 should not change if called again for index 1."""
        manager = ArtifactManager()
        step = create_fake_step(TEST_STEP_ID)
        ids_first = manager.accumulate_screenshot_to_step_archive(
            step=step, screenshots=[b"a"], artifact_type=ArtifactType.SCREENSHOT_LLM
        )
        ids_second = manager.accumulate_screenshot_to_step_archive(
            step=step, screenshots=[b"b"], artifact_type=ArtifactType.SCREENSHOT_LLM
        )
        acc = manager._step_archives[step.step_id]
        # IDs from first call still intact in member_types
        assert any(m[2] == ids_first[0] for m in acc.member_types)
        # New ID from second call is different
        assert ids_second[0] != ids_first[0]


# ---------------------------------------------------------------------------
# ZIP building
# ---------------------------------------------------------------------------


class TestBuildZip:
    """Tests for ArtifactManager._build_zip."""

    def test_zip_contains_all_entries(self) -> None:
        entries = {
            "scrape.html": b"<html/>",
            "element_tree.json": b"[]",
            "screenshot_llm_0.png": b"\x89PNG",
        }
        zip_bytes = ArtifactManager._build_zip(entries)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            assert set(zf.namelist()) == set(entries.keys())

    def test_zip_text_entries_are_readable(self) -> None:
        entries = {"llm_prompt.txt": b"you are a bot", "id_css_map.json": b'{"a":"b"}'}
        zip_bytes = ArtifactManager._build_zip(entries)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            assert zf.read("llm_prompt.txt") == b"you are a bot"
            assert zf.read("id_css_map.json") == b'{"a":"b"}'

    def test_zip_png_entries_round_trip(self) -> None:
        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        entries = {"screenshot_action_0.png": fake_png}
        zip_bytes = ArtifactManager._build_zip(entries)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            assert zf.read("screenshot_action_0.png") == fake_png

    def test_png_entries_use_stored_compression(self) -> None:
        """PNGs should use ZIP_STORED (no double-compression)."""
        entries = {"screenshot_llm_0.png": b"\x89PNG" + b"\x00" * 50}
        zip_bytes = ArtifactManager._build_zip(entries)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            info = zf.getinfo("screenshot_llm_0.png")
            assert info.compress_type == zipfile.ZIP_STORED

    def test_text_entries_use_deflate_compression(self) -> None:
        """Text entries should be deflate-compressed."""
        entries = {"llm_prompt.txt": b"a" * 1000}
        zip_bytes = ArtifactManager._build_zip(entries)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            info = zf.getinfo("llm_prompt.txt")
            assert info.compress_type == zipfile.ZIP_DEFLATED

    def test_empty_entries_produces_valid_zip(self) -> None:
        zip_bytes = ArtifactManager._build_zip({})
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            assert zf.namelist() == []

    def test_zip_entry_uses_stored_for_already_zipped(self) -> None:
        """Nested .zip entries should use ZIP_STORED."""
        entries = {"trace.zip": b"PK" + b"\x00" * 20}
        zip_bytes = ArtifactManager._build_zip(entries)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            info = zf.getinfo("trace.zip")
            assert info.compress_type == zipfile.ZIP_STORED


# ---------------------------------------------------------------------------
# get_or_create isolation
# ---------------------------------------------------------------------------


class TestGetOrCreateStepArchive:
    def test_separate_steps_get_separate_accumulators(self) -> None:
        manager = ArtifactManager()
        step1 = create_fake_step(TEST_STEP_ID)
        step2 = create_fake_step(TEST_STEP_ID_2)
        acc1 = manager._get_or_create_step_archive(step1, None, None, None)
        acc2 = manager._get_or_create_step_archive(step2, None, None, None)
        assert acc1 is not acc2
        assert acc1.step.step_id == TEST_STEP_ID
        assert acc2.step.step_id == TEST_STEP_ID_2

    def test_same_step_returns_same_accumulator(self) -> None:
        manager = ArtifactManager()
        step = create_fake_step(TEST_STEP_ID)
        acc_first = manager._get_or_create_step_archive(step, None, None, None)
        acc_second = manager._get_or_create_step_archive(step, None, None, None)
        assert acc_first is acc_second


# ---------------------------------------------------------------------------
# Full accumulation → ZIP round-trip (no S3 / DB)
# ---------------------------------------------------------------------------


class TestArchiveRoundTrip:
    """Verify that data accumulated across multiple helpers can be retrieved from the built ZIP."""

    def test_full_step_archive_round_trip(self) -> None:
        manager = ArtifactManager()
        step = create_fake_step(TEST_STEP_ID)

        manager.accumulate_scrape_to_archive(
            step=step,
            html=b"<html>page</html>",
            id_css_map=b'{"el1": "#id1"}',
            id_frame_map=b"{}",
            element_tree=b'[{"tag":"div"}]',
            element_tree_trimmed=b'[{"tag":"div"}]',
            element_tree_in_prompt=b"div#id1",
        )
        manager.accumulate_llm_call_to_archive(
            step=step,
            prompt=b"what should i click?",
            response=b'{"choices":[{"message":{"content":"click button"}}]}',
            parsed_response=b'{"actions":[{"action_type":"click"}]}',
        )
        manager.accumulate_action_html_to_archive(step=step, html_action=b"<html>after click</html>")
        screenshot_ids = manager.accumulate_screenshot_to_step_archive(
            step=step, screenshots=[b"\x89PNGscreenshot"], artifact_type=ArtifactType.SCREENSHOT_LLM
        )

        acc = manager._step_archives[step.step_id]
        zip_bytes = manager._build_zip(acc.entries)

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = set(zf.namelist())
            assert "scrape.html" in names
            assert "id_css_map.json" in names
            assert "llm_prompt_0.txt" in names
            assert "llm_response_0.json" in names
            assert "llm_response_parsed_0.json" in names
            assert "html_action_0.html" in names
            assert "screenshot_llm_0.png" in names

            assert zf.read("scrape.html") == b"<html>page</html>"
            assert zf.read("llm_prompt_0.txt") == b"what should i click?"
            assert zf.read("screenshot_llm_0.png") == b"\x89PNGscreenshot"

        assert len(screenshot_ids) == 1


class TestQueueActionScreenshotUpdate:
    """Tests for ArtifactManager.queue_action_screenshot_update."""

    def test_queues_update_when_archive_exists(self) -> None:
        """Pending update is appended to the accumulator for later flush."""
        step = create_fake_step(TEST_STEP_ID)
        manager = ArtifactManager()
        manager.accumulate_screenshot_to_step_archive(
            step=step,
            screenshots=[b"\x89PNGdata"],
            artifact_type=ArtifactType.SCREENSHOT_ACTION,
        )
        manager.queue_action_screenshot_update(
            step=step,
            organization_id="org_1",
            action_id="action_1",
            artifact_id="art_1",
        )
        acc = manager._step_archives[step.step_id]
        assert acc.pending_action_screenshot_updates == [("org_1", "action_1", "art_1")]

    def test_multiple_updates_are_ordered(self) -> None:
        """Each action in the step appends its own pending update in call order."""
        step = create_fake_step(TEST_STEP_ID)
        manager = ArtifactManager()
        manager.accumulate_screenshot_to_step_archive(
            step=step,
            screenshots=[b"png0", b"png1"],
            artifact_type=ArtifactType.SCREENSHOT_ACTION,
        )
        manager.queue_action_screenshot_update(
            step=step, organization_id="org_1", action_id="act_0", artifact_id="aid_0"
        )
        manager.queue_action_screenshot_update(
            step=step, organization_id="org_1", action_id="act_1", artifact_id="aid_1"
        )
        acc = manager._step_archives[step.step_id]
        assert acc.pending_action_screenshot_updates == [
            ("org_1", "act_0", "aid_0"),
            ("org_1", "act_1", "aid_1"),
        ]

    def test_no_crash_when_archive_missing(self, caplog: pytest.LogCaptureFixture) -> None:
        """If the accumulator is gone (e.g. already flushed), logs a warning and returns."""
        import logging

        step = create_fake_step(TEST_STEP_ID)
        manager = ArtifactManager()
        # No archive created — simulates calling after flush or discard
        with caplog.at_level(logging.WARNING):
            manager.queue_action_screenshot_update(
                step=step,
                organization_id="org_1",
                action_id="action_1",
                artifact_id="art_1",
            )
        assert any("no step archive found" in r.message for r in caplog.records)

    def test_task_archive_entries_helper(self) -> None:
        """Verify create_task_archive entries dict structure is correct for common types."""
        entries: dict[str, tuple[ArtifactType, bytes]] = {
            "har.har": (ArtifactType.HAR, b'{"log":{}}'),
            "browser_console.log": (ArtifactType.BROWSER_CONSOLE_LOG, b"[info] loaded"),
            "trace.zip": (ArtifactType.TRACE, b"PK\x03\x04"),
        }
        zip_entries = {filename: data for filename, (_, data) in entries.items()}
        zip_bytes = ArtifactManager._build_zip(zip_entries)

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            assert set(zf.namelist()) == {"har.har", "browser_console.log", "trace.zip"}
            assert zf.read("browser_console.log") == b"[info] loaded"


# ---------------------------------------------------------------------------
# flush_step_archive — per-step early flush
# ---------------------------------------------------------------------------


def _make_app_mocks() -> tuple[MagicMock, MagicMock]:
    """Return (mock_storage, mock_database) with async store/create methods."""
    mock_storage = MagicMock()
    mock_storage.build_uri.return_value = "s3://bucket/v1/test/tsk_123/01_0_step_id/archive.zip"
    mock_storage.store_artifact = AsyncMock()

    mock_database = MagicMock()
    mock_database.bulk_create_artifacts = AsyncMock()
    mock_database.update_action_screenshot_artifact_id = AsyncMock()
    return mock_storage, mock_database


class TestFlushStepArchive:
    """Tests for ArtifactManager.flush_step_archive (per-step early flush)."""

    @pytest.mark.asyncio
    async def test_flush_uploads_zip_and_creates_db_rows(self) -> None:
        """Flushing a populated accumulator should call store_artifact and bulk_create_artifacts."""
        mock_storage, mock_database = _make_app_mocks()
        manager = ArtifactManager()
        step = create_fake_step(TEST_STEP_ID)

        manager.accumulate_scrape_to_archive(
            step=step,
            html=b"<html/>",
            id_css_map=b"{}",
            id_frame_map=b"{}",
            element_tree=b"[]",
            element_tree_trimmed=b"[]",
            element_tree_in_prompt=b"",
        )

        with patch("skyvern.forge.sdk.artifact.manager.app") as mock_app:
            mock_app.STORAGE = mock_storage
            mock_app.DATABASE = mock_database
            await manager.flush_step_archive(step.step_id)

        mock_storage.store_artifact.assert_awaited_once()
        mock_database.bulk_create_artifacts.assert_awaited_once()
        # The artifact list should include the parent + 6 member rows (scrape produces 6 entries)
        call_args = mock_database.bulk_create_artifacts.call_args[0][0]
        assert len(call_args) == 7  # 1 parent + 6 members

    @pytest.mark.asyncio
    async def test_flush_removes_accumulator_from_dict(self) -> None:
        """After flush the accumulator should be gone from _step_archives."""
        mock_storage, mock_database = _make_app_mocks()
        manager = ArtifactManager()
        step = create_fake_step(TEST_STEP_ID)

        manager.accumulate_scrape_to_archive(
            step=step,
            html=b"<html/>",
            id_css_map=b"{}",
            id_frame_map=b"{}",
            element_tree=b"[]",
            element_tree_trimmed=b"[]",
            element_tree_in_prompt=b"",
        )
        assert step.step_id in manager._step_archives

        with patch("skyvern.forge.sdk.artifact.manager.app") as mock_app:
            mock_app.STORAGE = mock_storage
            mock_app.DATABASE = mock_database
            await manager.flush_step_archive(step.step_id)

        assert step.step_id not in manager._step_archives

    @pytest.mark.asyncio
    async def test_flush_is_idempotent_noop_on_second_call(self) -> None:
        """Calling flush_step_archive twice for the same step_id should not error or double-upload."""
        mock_storage, mock_database = _make_app_mocks()
        manager = ArtifactManager()
        step = create_fake_step(TEST_STEP_ID)

        manager.accumulate_scrape_to_archive(
            step=step,
            html=b"<html/>",
            id_css_map=b"{}",
            id_frame_map=b"{}",
            element_tree=b"[]",
            element_tree_trimmed=b"[]",
            element_tree_in_prompt=b"",
        )

        with patch("skyvern.forge.sdk.artifact.manager.app") as mock_app:
            mock_app.STORAGE = mock_storage
            mock_app.DATABASE = mock_database
            await manager.flush_step_archive(step.step_id)
            await manager.flush_step_archive(step.step_id)  # second call — no-op

        # store_artifact and bulk_create_artifacts should only be called once
        assert mock_storage.store_artifact.await_count == 1
        assert mock_database.bulk_create_artifacts.await_count == 1

    @pytest.mark.asyncio
    async def test_flush_nonexistent_step_id_is_noop(self) -> None:
        """Flushing a step_id with no accumulator should do nothing without raising."""
        mock_storage, mock_database = _make_app_mocks()
        manager = ArtifactManager()

        with patch("skyvern.forge.sdk.artifact.manager.app") as mock_app:
            mock_app.STORAGE = mock_storage
            mock_app.DATABASE = mock_database
            await manager.flush_step_archive("nonexistent_step_id")

        mock_storage.store_artifact.assert_not_awaited()
        mock_database.bulk_create_artifacts.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_flush_applies_pending_screenshot_updates(self) -> None:
        """Deferred action.screenshot_artifact_id updates are applied during flush."""
        mock_storage, mock_database = _make_app_mocks()
        manager = ArtifactManager()
        step = create_fake_step(TEST_STEP_ID)

        manager.accumulate_screenshot_to_step_archive(
            step=step,
            screenshots=[b"\x89PNGdata"],
            artifact_type=ArtifactType.SCREENSHOT_ACTION,
        )
        manager.queue_action_screenshot_update(
            step=step,
            organization_id="org_1",
            action_id="action_1",
            artifact_id="art_1",
        )

        with patch("skyvern.forge.sdk.artifact.manager.app") as mock_app:
            mock_app.STORAGE = mock_storage
            mock_app.DATABASE = mock_database
            await manager.flush_step_archive(step.step_id)

        mock_database.update_action_screenshot_artifact_id.assert_awaited_once_with(
            organization_id="org_1",
            action_id="action_1",
            screenshot_artifact_id="art_1",
        )

    @pytest.mark.asyncio
    async def test_wait_for_upload_aiotasks_finds_no_step_archives_after_per_step_flush(self) -> None:
        """After per-step flushes, wait_for_upload_aiotasks should have nothing left to flush."""
        mock_storage, mock_database = _make_app_mocks()
        manager = ArtifactManager()
        step = create_fake_step(TEST_STEP_ID)

        manager.accumulate_scrape_to_archive(
            step=step,
            html=b"<html/>",
            id_css_map=b"{}",
            id_frame_map=b"{}",
            element_tree=b"[]",
            element_tree_trimmed=b"[]",
            element_tree_in_prompt=b"",
        )

        with patch("skyvern.forge.sdk.artifact.manager.app") as mock_app:
            mock_app.STORAGE = mock_storage
            mock_app.DATABASE = mock_database
            # Simulate per-step flush right after step completes
            await manager.flush_step_archive(step.step_id)
            # Reset call counts to detect any additional calls from wait_for_upload_aiotasks
            mock_storage.store_artifact.reset_mock()
            mock_database.bulk_create_artifacts.reset_mock()
            # Simulate the end-of-task flush fallback
            await manager.wait_for_upload_aiotasks([step.task_id])

        # The fallback should find nothing to flush — no extra uploads
        mock_storage.store_artifact.assert_not_awaited()
        mock_database.bulk_create_artifacts.assert_not_awaited()
