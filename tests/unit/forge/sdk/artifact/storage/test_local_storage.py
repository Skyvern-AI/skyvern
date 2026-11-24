import asyncio
import json
import os
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zipfile import is_zipfile

import pytest
from freezegun import freeze_time

from skyvern.config import settings
from skyvern.forge.sdk.artifact.models import ArtifactType, LogEntityType
from skyvern.forge.sdk.artifact.storage import local as local_module
from skyvern.forge.sdk.artifact.storage.local import LocalStorage
from skyvern.forge.sdk.encrypt.files import ArtifactDecryptionError
from skyvern.webeye.session_cookies import SESSION_COOKIES_FILENAME, read_persisted_session_cookies
from tests.unit.forge.sdk.artifact.storage.test_helpers import (
    artifact_root,
    create_fake_for_ai_suggestion,
    create_fake_step,
    create_fake_task_v2,
    create_fake_thought,
    create_fake_workflow_run_block,
)

# Test constants
TEST_BUCKET = "test-skyvern-bucket"
TEST_ORGANIZATION_ID = "test-org-123"
TEST_TASK_ID = "tsk_123456789"
TEST_STEP_ID = "step_123456789"
TEST_WORKFLOW_RUN_ID = "wfr_123456789"
TEST_BLOCK_ID = "block_123456789"
TEST_AI_SUGGESTION_ID = "ai_sugg_test_123"


@pytest.fixture
def local_storage() -> LocalStorage:
    return LocalStorage()


@freeze_time("2025-06-09T12:00:00")
class TestLocalStorageBuildURIs:
    def test_build_uri(self, local_storage: LocalStorage) -> None:
        step = create_fake_step(TEST_STEP_ID)
        uri = local_storage.build_uri(
            organization_id=TEST_ORGANIZATION_ID,
            artifact_id="artifact123",
            step=step,
            artifact_type=ArtifactType.LLM_PROMPT,
        )
        assert (
            uri
            == f"file://{local_storage.artifact_path}/{TEST_ORGANIZATION_ID}/{TEST_TASK_ID}/01_0_{TEST_STEP_ID}/2025-06-09T12:00:00_artifact123_llm_prompt.txt"
        )

    def test_build_log_uri(self, local_storage: LocalStorage) -> None:
        uri = local_storage.build_log_uri(
            organization_id=TEST_ORGANIZATION_ID,
            log_entity_type=LogEntityType.WORKFLOW_RUN_BLOCK,
            log_entity_id="log_id",
            artifact_type=ArtifactType.SKYVERN_LOG,
        )
        assert (
            uri
            == f"file://{local_storage.artifact_path}/logs/workflow_run_block/log_id/2025-06-09T12:00:00_skyvern_log.log"
        )

    def test_build_thought_uri(self, local_storage: LocalStorage) -> None:
        thought = create_fake_thought("cruise123", "thought123")
        uri = local_storage.build_thought_uri(
            organization_id=TEST_ORGANIZATION_ID,
            artifact_id="artifact123",
            thought=thought,
            artifact_type=ArtifactType.VISIBLE_ELEMENTS_TREE,
        )
        assert (
            uri
            == f"file://{local_storage.artifact_path}/{settings.ENV}/{TEST_ORGANIZATION_ID}/tasks/cruise123/thought123/2025-06-09T12:00:00_artifact123_visible_elements_tree.json"
        )

    def test_build_task_v2_uri(self, local_storage: LocalStorage) -> None:
        task_v2 = create_fake_task_v2("cruise123")
        uri = local_storage.build_task_v2_uri(
            organization_id=TEST_ORGANIZATION_ID,
            artifact_id="artifact123",
            task_v2=task_v2,
            artifact_type=ArtifactType.HTML_ACTION,
        )
        assert (
            uri
            == f"file://{local_storage.artifact_path}/{settings.ENV}/{TEST_ORGANIZATION_ID}/observers/cruise123/2025-06-09T12:00:00_artifact123_html_action.html"
        )

    def test_build_workflow_run_block_uri(self, local_storage: LocalStorage) -> None:
        workflow_run_block = create_fake_workflow_run_block(TEST_WORKFLOW_RUN_ID, TEST_BLOCK_ID)
        uri = local_storage.build_workflow_run_block_uri(
            organization_id=TEST_ORGANIZATION_ID,
            artifact_id="artifact123",
            workflow_run_block=workflow_run_block,
            artifact_type=ArtifactType.HAR,
        )
        assert (
            uri
            == f"file://{local_storage.artifact_path}/{settings.ENV}/{TEST_ORGANIZATION_ID}/workflow_runs/{TEST_WORKFLOW_RUN_ID}/{TEST_BLOCK_ID}/2025-06-09T12:00:00_artifact123_har.har"
        )

    def test_build_ai_suggestion_uri(self, local_storage: LocalStorage) -> None:
        ai_suggestion = create_fake_for_ai_suggestion(TEST_AI_SUGGESTION_ID)
        uri = local_storage.build_ai_suggestion_uri(
            organization_id=TEST_ORGANIZATION_ID,
            artifact_id="artifact123",
            ai_suggestion=ai_suggestion,
            artifact_type=ArtifactType.SCREENSHOT_LLM,
        )
        assert (
            uri
            == f"file://{local_storage.artifact_path}/{settings.ENV}/{TEST_ORGANIZATION_ID}/ai_suggestions/{TEST_AI_SUGGESTION_ID}/2025-06-09T12:00:00_artifact123_screenshot_llm.png"
        )


@pytest.mark.asyncio
async def test_delete_browser_profile_hard_delete_raises_on_failure(
    local_storage: LocalStorage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "BROWSER_SESSION_BASE_PATH", str(tmp_path))
    (tmp_path / TEST_ORGANIZATION_ID / "profiles" / "bp_1").mkdir(parents=True)
    with patch("skyvern.forge.sdk.artifact.storage.local.shutil.rmtree", side_effect=OSError("disk")):
        # hard_delete surfaces the failure so the caller reports reap_failed, not a false erasure.
        with pytest.raises(OSError):
            await local_storage.delete_browser_profile(TEST_ORGANIZATION_ID, "bp_1", hard_delete=True)
        # default delete stays best-effort (swallowed).
        await local_storage.delete_browser_profile(TEST_ORGANIZATION_ID, "bp_1", hard_delete=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read_only", "encrypted", "scenario"),
    [
        pytest.param(False, False, "legacy", id="legacy-writable"),
        pytest.param(True, False, "legacy", id="legacy-read-only"),
        pytest.param(False, True, "at-rest", id="encrypted-at-rest"),
        pytest.param(False, True, "migrate-live", id="migration-live-writer-off"),
        pytest.param(False, True, "migrate-copy", id="migration-copy-writer-on"),
        pytest.param(False, True, "migrate-missing", id="migration-missing-writer-on"),
        pytest.param(False, True, "disabled-update", id="encrypted-save-with-writes-disabled"),
        pytest.param(False, True, "competing-seal", id="legacy-copy-competing-seal"),
        pytest.param(False, True, "competing-seal-failure", id="legacy-copy-failure-competing-seal"),
        pytest.param(False, True, "corrupt", id="encrypted-corruption-refuses-legacy"),
        pytest.param(False, True, "replace-failure", id="encrypted-atomic-save"),
        pytest.param(False, True, "cancelled", id="encrypted-cancel-save"),
    ],
)
async def test_browser_profile_cookie_state_survives_storage(
    local_storage: LocalStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read_only: bool,
    encrypted: bool,
    scenario: str,
) -> None:
    storage_root = tmp_path / "sessions"
    monkeypatch.setattr(settings, "BROWSER_SESSION_BASE_PATH", str(storage_root))
    monkeypatch.setattr(settings, "TEMP_PATH", str(tmp_path / "temp"))
    monkeypatch.setattr(settings, "ENABLE_ENCRYPTION", True)
    monkeypatch.setattr(settings, "SKYVERN_ENCRYPT_BROWSER_PROFILES", encrypted)
    stored = storage_root / TEST_ORGANIZATION_ID / "profiles" / "bp_1"
    source = tmp_path / "profile" if encrypted else stored
    (source / "Default").mkdir(parents=True)
    cookie_value = "synthetic-profile-login-cookie"
    cookie = {
        "name": "session",
        "value": cookie_value,
        "domain": "example.invalid",
        "path": "/",
        "expires": -1,
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
    }
    if encrypted:
        monkeypatch.setattr(settings, "SKYVERN_ARTIFACT_ENCRYPTION_KEYS", (artifact_root(),))
        (source / "Default" / "Preferences").write_text("{}", encoding="utf-8")
        (source / SESSION_COOKIES_FILENAME).write_text(json.dumps([cookie]), encoding="utf-8")
        await local_storage.store_browser_profile(TEST_ORGANIZATION_ID, "bp_1", str(source))
    else:
        (source / "Default" / "Cookies").write_text("session=original")

    if read_only and os.name != "nt":
        outside = tmp_path / "outside"
        outside.write_bytes(b"external")
        (stored / "Default" / "Link").symlink_to(outside)

    retrieve = local_storage.retrieve_browser_profile_copy if read_only else local_storage.retrieve_browser_profile
    browser_dir = await retrieve(TEST_ORGANIZATION_ID, "bp_1")
    assert browser_dir is not None
    assert not (Path(browser_dir) / "Default" / "Link").exists()
    if encrypted:
        assert read_persisted_session_cookies(browser_dir) == [cookie]
        stored_files = [path for path in storage_root.rglob("*") if path.is_file()]
        assert stored_files
        for path in stored_files:
            assert not is_zipfile(path)
            assert cookie_value.encode() not in path.read_bytes(), f"Plaintext cookie in {path}"
        assert len(stored_files) == 1
        sealed = stored_files[0]
        previous = sealed.read_bytes()
        if scenario == "at-rest":
            return
        if scenario.startswith("migrate-"):
            shutil.rmtree(browser_dir)
            pending = sealed.rename(tmp_path / "pending")
            stored.mkdir()
            (stored / "legacy-only").touch()
            stat_path, copy_directory = local_storage._profile_stat, local_storage._copy_directory_best_effort

            def publish_seal() -> None:
                pending.replace(sealed)
                shutil.rmtree(stored)

            def observe_legacy(path: Path) -> os.stat_result | None:
                observed = stat_path(path)
                if path == stored and observed is not None:
                    publish_seal()
                return observed

            def copy_legacy(src: Path, dest: Path, *, private_copy: bool) -> None:
                if scenario == "migrate-missing":
                    publish_seal()
                copy_directory(src, dest, private_copy=private_copy)
                if scenario == "migrate-copy":
                    publish_seal()

            if scenario == "migrate-live":
                monkeypatch.setattr(settings, "SKYVERN_ENCRYPT_BROWSER_PROFILES", False)
                monkeypatch.setattr(local_storage, "_profile_stat", observe_legacy)
            else:
                monkeypatch.setattr(local_storage, "_copy_directory_best_effort", copy_legacy)
            snapshot = await local_storage.retrieve_browser_profile_snapshot(TEST_ORGANIZATION_ID, "bp_1")
            assert snapshot is not None and snapshot.encrypted
            assert read_persisted_session_cookies(snapshot.directory) == [cookie]
            assert list((tmp_path / "temp").glob("skyvern_browser_profile_*")) == [Path(snapshot.directory)]
            return

        monkeypatch.setattr(settings, "SKYVERN_ENCRYPT_BROWSER_PROFILES", False)
        if scenario == "corrupt":
            stored.mkdir()
            (stored / SESSION_COOKIES_FILENAME).write_text(json.dumps([cookie]), encoding="utf-8")
            sealed.write_bytes(previous[:-1])
            with pytest.raises(ArtifactDecryptionError):
                await local_storage.retrieve_browser_profile(TEST_ORGANIZATION_ID, "bp_1")
            assert not list((tmp_path / "temp").glob("skyvern_profile_archive_*"))
            return

        rotated = {**cookie, "value": "synthetic-profile-rotated-cookie"}
        (source / SESSION_COOKIES_FILENAME).write_text(json.dumps([rotated]), encoding="utf-8")
        if scenario == "disabled-update":
            await local_storage.store_browser_profile(TEST_ORGANIZATION_ID, "bp_1", str(source))
            assert not stored.exists()
            assert not is_zipfile(sealed)
            reopened = await local_storage.retrieve_browser_profile(TEST_ORGANIZATION_ID, "bp_1")
            assert read_persisted_session_cookies(reopened) == [rotated]
            return
        if scenario.startswith("competing-seal"):
            sealed.unlink()
            copy = local_storage._copy_directory_best_effort
            failed_copy = scenario == "competing-seal-failure"
            copy_error = OSError("legacy copy failed after writing cookies")

            def seal_during_copy(src: Path, dest: Path) -> None:
                sealed.write_bytes(previous)
                if dest.exists():
                    shutil.rmtree(dest)
                if failed_copy:
                    dest.mkdir()
                    shutil.copyfile(src / SESSION_COOKIES_FILENAME, dest / SESSION_COOKIES_FILENAME)
                    raise copy_error
                copy(src, dest)

            monkeypatch.setattr(local_storage, "_copy_directory_best_effort", seal_during_copy)
            with pytest.raises(OSError if failed_copy else RuntimeError) as caught:
                await local_storage.store_browser_profile(TEST_ORGANIZATION_ID, "bp_1", str(source))
            if failed_copy:
                assert caught.value is copy_error
            else:
                assert "encrypted by another writer" in str(caught.value)
            assert not stored.exists()
            assert sealed.read_bytes() == previous
            return
        if scenario == "replace-failure":
            replace = os.replace

            def fail_publish(source: Path, destination: Path) -> None:
                if Path(destination) == sealed:
                    raise OSError("publication failed")
                replace(source, destination)

            monkeypatch.setattr(local_module.os, "replace", fail_publish)
            with pytest.raises(OSError, match="publication failed"):
                await local_storage.store_browser_profile(TEST_ORGANIZATION_ID, "bp_1", str(source))
        else:
            await _cancel_staged_local_profile(local_storage, source, monkeypatch)
        assert sealed.read_bytes() == previous
        assert list(sealed.parent.iterdir()) == [sealed]
        assert not list((tmp_path / "temp").glob("skyvern_profile_archive_*"))
    else:
        (Path(browser_dir) / "Default" / "Cookies").write_text("session=rotated")
        assert (stored / "Default" / "Cookies").read_text() == ("session=original" if read_only else "session=rotated")


async def _cancel_staged_local_profile(storage: LocalStorage, source: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    staged = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    stage = local_module.stage_profile_archive
    staged_paths: list[Path] = []

    def pause_stage(*args, **kwargs) -> Path:
        staged_paths.append(stage(*args, **kwargs))
        loop.call_soon_threadsafe(staged.set)
        assert release.wait(5)
        return staged_paths[-1]

    monkeypatch.setattr(local_module, "stage_profile_archive", pause_stage)
    task = asyncio.create_task(storage.store_browser_profile(TEST_ORGANIZATION_ID, "bp_1", str(source)))
    try:
        await asyncio.wait_for(staged.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        assert staged_paths[0].is_file()
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_delete_legacy_file_deletes_managed_artifact(tmp_path: Path) -> None:
    storage = LocalStorage(artifact_path=str(tmp_path))
    artifact_paths = [
        tmp_path / TEST_ORGANIZATION_ID / "tasks" / "legacy-artifact.png",
        tmp_path / settings.ENV / TEST_ORGANIZATION_ID / "tasks" / "artifact.png",
    ]
    for artifact_path in artifact_paths:
        artifact_path.parent.mkdir(parents=True)
        artifact_path.write_bytes(b"artifact")
        await storage.delete_legacy_file(organization_id=TEST_ORGANIZATION_ID, uri=f"file://{artifact_path}")
        await storage.delete_legacy_file(organization_id=TEST_ORGANIZATION_ID, uri=f"file://{artifact_path}")

    assert not any(artifact_path.exists() for artifact_path in artifact_paths)


@pytest.mark.asyncio
async def test_delete_legacy_file_rejects_other_organization_path(tmp_path: Path) -> None:
    storage = LocalStorage(artifact_path=str(tmp_path))
    artifact_path = tmp_path / "other-organization" / "tasks" / "artifact.png"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(b"artifact")

    with pytest.raises(PermissionError):
        await storage.delete_legacy_file(organization_id=TEST_ORGANIZATION_ID, uri=f"file://{artifact_path}")

    assert artifact_path.exists()


@pytest.mark.asyncio
async def test_a_managed_png_presigns_to_its_own_uri_and_another_organization_is_refused(tmp_path: Path) -> None:
    storage = LocalStorage(artifact_path=str(tmp_path))
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 32
    own = tmp_path / settings.ENV / TEST_ORGANIZATION_ID / "uploads" / "shot.png"
    other = tmp_path / settings.ENV / "other-organization" / "uploads" / "shot.png"
    for path in (own, other):
        path.parent.mkdir(parents=True)
        path.write_bytes(png)

    url = await storage.presign_managed_file(
        f"file://{own}", TEST_ORGANIZATION_ID, expiry_seconds=60, inline_content_type="image/png"
    )

    assert url == f"file://{own}"
    assert await storage.read_managed_file_head(url, TEST_ORGANIZATION_ID, 8) == png[:8]
    with pytest.raises(PermissionError):
        await storage.presign_managed_file(
            f"file://{other}", TEST_ORGANIZATION_ID, expiry_seconds=60, inline_content_type="image/png"
        )
    with pytest.raises(PermissionError):
        await storage.read_managed_file_head(f"file://{other}", TEST_ORGANIZATION_ID, 8)


@pytest.mark.asyncio
async def test_get_downloaded_files_carries_local_artifact_attribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = TEST_WORKFLOW_RUN_ID
    download_path = tmp_path / run_id / "report.pdf"
    download_path.parent.mkdir(parents=True)
    download_path.write_bytes(b"report")
    uri = f"file://{download_path}"
    artifact = SimpleNamespace(
        artifact_id="artifact_download_1",
        uri=uri,
        modified_at=None,
    )
    list_artifacts = AsyncMock(return_value=[artifact])
    monkeypatch.setattr(settings, "DOWNLOAD_PATH", str(tmp_path))

    with patch("skyvern.forge.sdk.artifact.storage.local.app") as mock_app:
        mock_app.DATABASE.artifacts.list_artifacts_for_run_by_type = list_artifacts
        files = await LocalStorage().get_downloaded_files(TEST_ORGANIZATION_ID, run_id)

    assert len(files) == 1
    assert files[0].artifact_id == "artifact_download_1"
    assert files[0].modified_at is not None
    list_artifacts.assert_awaited_once_with(
        run_id=run_id,
        organization_id=TEST_ORGANIZATION_ID,
        artifact_type=ArtifactType.DOWNLOAD,
    )
