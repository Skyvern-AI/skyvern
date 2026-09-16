import os
import shutil
from datetime import UTC, datetime
from pathlib import Path, PurePath
from typing import BinaryIO
from urllib.parse import quote

import aiofiles
import structlog

from skyvern.config import settings
from skyvern.constants import DOWNLOAD_FILE_PREFIX
from skyvern.exceptions import DownloadSaveIncompleteError
from skyvern.forge import app
from skyvern.forge.sdk.api.files import (
    calculate_sha256_for_file,
    get_download_dir,
    get_skyvern_temp_dir,
    parse_uri_to_path,
    register_local_download_root,
    wait_for_pending_extension_rename,
)
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType, LogEntityType
from skyvern.forge.sdk.artifact.storage.base import (
    FILE_EXTENTSION_MAP,
    BaseStorage,
    download_checksums_by_uri,
    is_file_from_retry_attempt,
    resolve_download_attempt_fail_open,
)
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.ai_suggestions import AISuggestion
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.task_v2 import TaskV2, Thought
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
from skyvern.forge.sdk.workflow.loop_download_filter import DownloadedFileSignature, to_downloaded_file_signature
from skyvern.utils.script_file_paths import build_script_file_storage_uri
from skyvern.webeye.session_cookies import SESSION_COOKIES_FILENAME

LOG = structlog.get_logger()
WINDOWS = os.name == "nt"

# Live Chromium profiles carry runtime files that exist but can't be copied as regular files
# (Singleton sockets/locks). These are skipped during a store; a copy failure on any other file
# means an incomplete profile and is re-raised.
_TRANSIENT_PROFILE_FILES = {"RunningChromeVersion", "SingletonLock", "SingletonSocket", "SingletonCookie"}


def _safe_timestamp() -> str:
    ts = datetime.utcnow().isoformat()
    return ts.replace(":", "-") if WINDOWS else ts


def _windows_safe_filename(name: str) -> str:
    if not WINDOWS:
        return name
    invalid = '<>:"/\\|?*'
    name = "".join("-" if ch in invalid else ch for ch in name)
    return name.rstrip(" .")


_MAX_FILENAME_BYTES = 255


def bounded_basename(name: str) -> str:
    """Shorten ``name`` to the common per-component filesystem limit, keeping its start and extension.

    Uploads are stored as ``<file_id>_<original name>``, so the id prefix survives and keeps the
    shortened name unique. Truncation happens on whole characters so no multi-byte one is split.
    """
    if len(name.encode()) <= _MAX_FILENAME_BYTES:
        return name
    stem, ext = os.path.splitext(name)
    if len(ext.encode()) > 32:
        stem, ext = name, ""
    budget = _MAX_FILENAME_BYTES - len(ext.encode())
    return stem.encode()[:budget].decode(errors="ignore") + ext


def managed_file_uri(path: PurePath) -> str:
    """Build a file URI that parse_uri_to_path decodes back to the same path on every platform.

    Percent-encoding keeps a "#" or "?" in a filename from reading back as a fragment or query.
    Path.as_uri is avoided because it emits file:///C:/... on Windows, which that parser turns
    into /C:/... rather than a drive-qualified path. Forward slashes keep the filename as the
    URI's last segment, which download_file uses as the temporary file's name.
    """
    return "file://" + quote(path.as_posix())


class LocalStorage(BaseStorage):
    def __init__(self, artifact_path: str = settings.ARTIFACT_STORAGE_PATH) -> None:
        self.artifact_path = artifact_path
        register_local_download_root(os.path.join(self.artifact_path, "downloads"))

    def build_uri(self, *, organization_id: str, artifact_id: str, step: Step, artifact_type: ArtifactType) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        if WINDOWS:
            ts = _safe_timestamp()
            return f"file://{self.artifact_path}/{organization_id}/{step.task_id}/{step.order:02d}_{step.retry_index}_{step.step_id}/{ts}_{artifact_id}_{artifact_type}.{file_ext}"
        return f"file://{self.artifact_path}/{organization_id}/{step.task_id}/{step.order:02d}_{step.retry_index}_{step.step_id}/{datetime.utcnow().isoformat()}_{artifact_id}_{artifact_type}.{file_ext}"

    async def retrieve_global_workflows(self) -> list[str]:
        file_path = Path(f"{self.artifact_path}/{settings.ENV}/global_workflows.txt")
        self._create_directories_if_not_exists(file_path)
        if not file_path.exists():
            return []
        try:
            async with aiofiles.open(file_path, "r") as f:
                lines = await f.readlines()
                return [line.strip() for line in lines if line.strip()]
        except Exception:
            return []

    def build_log_uri(
        self, *, organization_id: str, log_entity_type: LogEntityType, log_entity_id: str, artifact_type: ArtifactType
    ) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        if WINDOWS:
            ts = _safe_timestamp()
            return f"file://{self.artifact_path}/logs/{log_entity_type}/{log_entity_id}/{ts}_{artifact_type}.{file_ext}"
        return f"file://{self.artifact_path}/logs/{log_entity_type}/{log_entity_id}/{datetime.utcnow().isoformat()}_{artifact_type}.{file_ext}"

    def build_thought_uri(
        self, *, organization_id: str, artifact_id: str, thought: Thought, artifact_type: ArtifactType
    ) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        if WINDOWS:
            ts = _safe_timestamp()
            return f"file://{self.artifact_path}/{settings.ENV}/{organization_id}/tasks/{thought.observer_cruise_id}/{thought.observer_thought_id}/{ts}_{artifact_id}_{artifact_type}.{file_ext}"
        return f"file://{self.artifact_path}/{settings.ENV}/{organization_id}/tasks/{thought.observer_cruise_id}/{thought.observer_thought_id}/{datetime.utcnow().isoformat()}_{artifact_id}_{artifact_type}.{file_ext}"

    def build_task_v2_uri(
        self, *, organization_id: str, artifact_id: str, task_v2: TaskV2, artifact_type: ArtifactType
    ) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        if WINDOWS:
            ts = _safe_timestamp()
            return f"file://{self.artifact_path}/{settings.ENV}/{organization_id}/observers/{task_v2.observer_cruise_id}/{ts}_{artifact_id}_{artifact_type}.{file_ext}"
        return f"file://{self.artifact_path}/{settings.ENV}/{organization_id}/observers/{task_v2.observer_cruise_id}/{datetime.utcnow().isoformat()}_{artifact_id}_{artifact_type}.{file_ext}"

    def build_workflow_run_block_uri(
        self,
        *,
        organization_id: str,
        artifact_id: str,
        workflow_run_block: WorkflowRunBlock,
        artifact_type: ArtifactType,
    ) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        if WINDOWS:
            ts = _safe_timestamp()
            return f"file://{self.artifact_path}/{settings.ENV}/{organization_id}/workflow_runs/{workflow_run_block.workflow_run_id}/{workflow_run_block.workflow_run_block_id}/{ts}_{artifact_id}_{artifact_type}.{file_ext}"
        return f"file://{self.artifact_path}/{settings.ENV}/{organization_id}/workflow_runs/{workflow_run_block.workflow_run_id}/{workflow_run_block.workflow_run_block_id}/{datetime.utcnow().isoformat()}_{artifact_id}_{artifact_type}.{file_ext}"

    def build_ai_suggestion_uri(
        self, *, organization_id: str, artifact_id: str, ai_suggestion: AISuggestion, artifact_type: ArtifactType
    ) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        if WINDOWS:
            ts = _safe_timestamp()
            return f"file://{self.artifact_path}/{settings.ENV}/{organization_id}/ai_suggestions/{ai_suggestion.ai_suggestion_id}/{ts}_{artifact_id}_{artifact_type}.{file_ext}"
        return f"file://{self.artifact_path}/{settings.ENV}/{organization_id}/ai_suggestions/{ai_suggestion.ai_suggestion_id}/{datetime.utcnow().isoformat()}_{artifact_id}_{artifact_type}.{file_ext}"

    def build_script_file_uri(
        self, *, organization_id: str, script_id: str, script_version: int, file_path: str
    ) -> str:
        return build_script_file_storage_uri(
            f"file://{self.artifact_path}/{settings.ENV}/{organization_id}",
            script_id=script_id,
            script_version=script_version,
            file_path=file_path,
        )

    async def store_artifact(
        self,
        artifact: Artifact,
        data: bytes,
        supersede_queued_prefixes: bool = False,
        prefix_uri: str | None = None,
    ) -> None:
        file_path = None
        try:
            file_path = Path(parse_uri_to_path(artifact.uri))
            if WINDOWS:
                file_path = file_path.with_name(_windows_safe_filename(file_path.name))
            self._create_directories_if_not_exists(file_path)
            async with aiofiles.open(file_path, "wb") as f:
                await f.write(data)
        except Exception:
            LOG.exception(
                "Failed to store artifact locally.",
                file_path=file_path,
                artifact=artifact,
            )

    async def store_artifact_from_path(self, artifact: Artifact, path: str) -> None:
        file_path = None
        try:
            file_path = Path(parse_uri_to_path(artifact.uri))
            if WINDOWS:
                file_path = file_path.with_name(_windows_safe_filename(file_path.name))
            self._create_directories_if_not_exists(file_path)
            Path(path).replace(file_path)
        except Exception:
            LOG.exception(
                "Failed to store artifact locally.",
                file_path=file_path,
                artifact=artifact,
            )

    async def retrieve_artifact(self, artifact: Artifact) -> bytes | None:
        file_path = None
        try:
            file_path = parse_uri_to_path(artifact.uri)
            async with aiofiles.open(file_path, "rb") as f:
                return await f.read()
        except Exception:
            LOG.exception(
                "Failed to retrieve local artifact.",
                file_path=file_path,
                artifact=artifact,
            )
            return None

    async def get_share_link(self, artifact: Artifact) -> str | None:
        return artifact.uri if artifact.uri else None

    async def get_share_links(self, artifacts: list[Artifact]) -> list[str] | None:
        return [artifact.uri for artifact in artifacts] or None

    async def save_streaming_file(self, organization_id: str, file_name: str) -> bool | None:
        return None

    async def get_streaming_file(self, organization_id: str, file_name: str) -> bytes | None:
        # make the directory if it doesn't exist
        Path(f"{get_skyvern_temp_dir()}/{organization_id}").mkdir(parents=True, exist_ok=True)
        file_path = Path(f"{get_skyvern_temp_dir()}/{organization_id}/{file_name}")
        try:
            async with aiofiles.open(file_path, "rb") as f:
                return await f.read()
        except Exception:
            return None

    def _drop_stale_session_sidecar(self, source_directory: Path, stored_folder_path: Path) -> None:
        # These stores overlay onto an existing dir, so a sidecar dropped at the source (session ended
        # with no session cookies) must also be cleared in the destination, else a dead session reinjects.
        if (source_directory / SESSION_COOKIES_FILENAME).exists():
            return
        (stored_folder_path / SESSION_COOKIES_FILENAME).unlink(missing_ok=True)

    def _copy_directory_best_effort(self, source_directory: Path, stored_folder_path: Path) -> None:
        # Source may be a live browser profile. Skip only transient runtime files: ones that vanish
        # mid-walk (FileNotFoundError) or Chrome's Singleton sockets/locks that can't be copied as
        # regular files. Re-raise anything else (e.g. ENOSPC/permission on Cookies or localStorage) so
        # a partial profile isn't silently stored and later reused as if it were valid.
        for root, _, files in os.walk(source_directory):
            for file in files:
                source_file_path = Path(root) / file
                target_file_path = stored_folder_path / source_file_path.relative_to(source_directory)
                try:
                    self._create_directories_if_not_exists(target_file_path)
                    shutil.copy2(source_file_path, target_file_path)
                except OSError as e:
                    if isinstance(e, FileNotFoundError) or file in _TRANSIENT_PROFILE_FILES:
                        LOG.debug(
                            "Skipped transient profile file while storing browser dir", path=str(source_file_path)
                        )
                    else:
                        raise

    async def store_browser_session(self, organization_id: str, workflow_permanent_id: str, directory: str) -> None:
        stored_folder_path = self._resolve_browser_storage_path(organization_id, workflow_permanent_id)
        if stored_folder_path is None:
            LOG.warning(
                "Refused to store browser session outside storage base path",
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
                base_path=settings.BROWSER_SESSION_BASE_PATH,
            )
            return
        source_directory = Path(directory).resolve()
        if source_directory == stored_folder_path:
            return
        self._create_directories_if_not_exists(stored_folder_path)
        LOG.info(
            "Storing browser session locally",
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
            directory=str(source_directory),
            browser_session_path=str(stored_folder_path),
        )

        self._drop_stale_session_sidecar(source_directory, stored_folder_path)
        self._copy_directory_best_effort(source_directory, stored_folder_path)

    async def retrieve_browser_session(self, organization_id: str, workflow_permanent_id: str) -> str | None:
        stored_folder_path = self._resolve_browser_storage_path(organization_id, workflow_permanent_id)
        if stored_folder_path is None:
            LOG.warning(
                "Refused to retrieve browser session outside storage base path",
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
                base_path=settings.BROWSER_SESSION_BASE_PATH,
            )
            return None
        if not stored_folder_path.exists():
            return None
        return str(stored_folder_path)

    async def delete_browser_session(self, organization_id: str, workflow_permanent_id: str) -> None:
        stored_folder_path = self._resolve_browser_storage_path(organization_id, workflow_permanent_id)
        if stored_folder_path is None:
            LOG.warning(
                "Refused to delete browser session outside storage base path",
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
                base_path=settings.BROWSER_SESSION_BASE_PATH,
            )
            return
        if not stored_folder_path.exists():
            return
        try:
            shutil.rmtree(stored_folder_path)
        except Exception:
            LOG.exception(
                "Failed to delete local browser session",
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
                path=str(stored_folder_path),
            )
            raise

    async def store_browser_profile(self, organization_id: str, profile_id: str, directory: str) -> None:
        """Store browser profile locally."""
        stored_folder_path = self._resolve_browser_storage_path(organization_id, "profiles", profile_id)
        if stored_folder_path is None:
            LOG.warning(
                "Refused to store browser profile outside storage base path",
                organization_id=organization_id,
                profile_id=profile_id,
                base_path=settings.BROWSER_SESSION_BASE_PATH,
            )
            return
        source_directory = Path(directory).resolve()
        if source_directory == stored_folder_path:
            return
        # True overwrite: drop any prior contents so a re-save can't leave stale cookies or
        # localStorage from the old session mixed into the refreshed profile. Let errors surface
        # rather than silently merging onto a half-deleted directory.
        if stored_folder_path.exists():
            shutil.rmtree(stored_folder_path)
        self._create_directories_if_not_exists(stored_folder_path)
        LOG.info(
            "Storing browser profile locally",
            organization_id=organization_id,
            profile_id=profile_id,
            directory=str(source_directory),
            browser_profile_path=str(stored_folder_path),
        )

        self._drop_stale_session_sidecar(source_directory, stored_folder_path)
        self._copy_directory_best_effort(source_directory, stored_folder_path)

    async def retrieve_browser_profile(self, organization_id: str, profile_id: str) -> str | None:
        """Retrieve browser profile from local storage."""
        stored_folder_path = self._resolve_browser_storage_path(organization_id, "profiles", profile_id)
        if stored_folder_path is None:
            LOG.warning(
                "Refused to retrieve browser profile outside storage base path",
                organization_id=organization_id,
                profile_id=profile_id,
                base_path=settings.BROWSER_SESSION_BASE_PATH,
            )
            return None
        if not stored_folder_path.exists():
            return None
        return str(stored_folder_path)

    async def browser_profile_exists(self, organization_id: str, profile_id: str) -> bool:
        """Non-destructive existence check — stat the stored directory, never retrieve/delete it."""
        stored_folder_path = self._resolve_browser_storage_path(organization_id, "profiles", profile_id)
        return stored_folder_path is not None and stored_folder_path.exists()

    async def delete_browser_profile(self, organization_id: str, profile_id: str, hard_delete: bool = False) -> None:
        """Delete a browser profile from local storage. Best-effort: a missing profile is a no-op.
        Local storage keeps no old versions, so a terminal dir delete is full erasure; under hard_delete
        a delete failure is RAISED (not swallowed) so the caller reports reap_failed, not a false erasure."""
        stored_folder_path = self._resolve_browser_storage_path(organization_id, "profiles", profile_id)
        if stored_folder_path is None:
            LOG.warning(
                "Refused to delete browser profile outside storage base path",
                organization_id=organization_id,
                profile_id=profile_id,
                base_path=settings.BROWSER_SESSION_BASE_PATH,
            )
            return
        if not stored_folder_path.exists():
            return
        try:
            shutil.rmtree(stored_folder_path)
        except Exception:
            LOG.exception(
                "Failed to delete local browser profile",
                organization_id=organization_id,
                profile_id=profile_id,
                path=str(stored_folder_path),
            )
            if hard_delete:
                raise

    async def save_downloaded_files(
        self,
        organization_id: str,
        run_id: str | None,
        *,
        attempt_number: int | None = None,
    ) -> None:
        if run_id is None:
            return
        download_dir = get_download_dir(run_id=run_id)
        files = os.listdir(download_dir)
        if not files:
            return
        owner_id, number, started_at = await resolve_download_attempt_fail_open(organization_id, run_id, attempt_number)
        destination = Path(self.artifact_path) / DOWNLOAD_FILE_PREFIX / settings.ENV / organization_id / run_id
        if number > 1:
            destination = destination / "attempts" / str(number)
        artifacts = await app.DATABASE.artifacts.list_artifacts_for_run_by_type(
            organization_id=organization_id, run_id=run_id, artifact_type=ArtifactType.DOWNLOAD
        )
        saved = download_checksums_by_uri(artifacts)
        skipped: list[str] = []
        for filename in files:
            source = Path(download_dir) / filename
            if not source.is_file():
                continue
            filename = await wait_for_pending_extension_rename(download_dir, filename)
            source = Path(download_dir) / filename
            if not source.is_file() or (number > 1 and not is_file_from_retry_attempt(str(source), started_at)):
                continue
            target = destination / filename
            uri = managed_file_uri(target.resolve())
            try:
                checksum = calculate_sha256_for_file(str(source))
                if saved.get(uri) == checksum and target.is_file():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                await app.ARTIFACT_MANAGER.create_download_artifact(
                    organization_id=organization_id,
                    run_id=run_id,
                    workflow_run_id=owner_id,
                    uri=uri,
                    filename=filename,
                    checksum=checksum,
                    file_size=target.stat().st_size,
                )
            except Exception:
                LOG.warning("Failed to preserve local download", filename=filename, run_id=run_id, exc_info=True)
                skipped.append(filename)
        if skipped:
            raise DownloadSaveIncompleteError(skipped)

    async def list_downloaded_files_in_browser_session(
        self, organization_id: str, browser_session_id: str
    ) -> list[str]:
        return []

    async def get_shared_downloaded_files_in_browser_session(
        self, organization_id: str, browser_session_id: str
    ) -> list[FileInfo]:
        return []

    async def list_downloading_files_in_browser_session(
        self, organization_id: str, browser_session_id: str
    ) -> list[str]:
        return []

    async def get_shared_recordings_in_browser_session(
        self, organization_id: str, browser_session_id: str
    ) -> list[FileInfo]:
        """Get recording files with URLs for a browser session from local storage.

        Videos are synced to the browser_sessions storage path when the session closes.
        """
        videos_base = (
            Path(self.artifact_path)
            / settings.ENV
            / organization_id
            / "browser_sessions"
            / browser_session_id
            / "videos"
        )

        if not videos_base.exists():
            return []

        file_uris: list[str] = []
        for root, _, files in os.walk(videos_base):
            for file in files:
                file_uris.append(f"file://{Path(root) / file}")

        if not file_uris:
            return []

        file_infos: list[FileInfo] = []
        for uri in file_uris:
            uri_lower = uri.lower()
            if not (uri_lower.endswith(".webm") or uri_lower.endswith(".mp4")):
                LOG.warning(
                    "Skipping recording file with unsupported extension",
                    uri=uri,
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                )
                continue

            file_path = parse_uri_to_path(uri)
            path_obj = Path(file_path)

            if not path_obj.exists():
                continue

            try:
                stat_result = path_obj.stat()
            except OSError:
                LOG.warning("Failed to stat local recording file", path=file_path, exc_info=True)
                continue
            file_size = stat_result.st_size
            if file_size == 0:
                continue

            # Return UTC-aware so consumers can safely compare against S3 LastModified
            # (also UTC-aware) without hitting naive-vs-aware TypeErrors.
            modified_at = datetime.fromtimestamp(stat_result.st_mtime, tz=UTC)
            checksum = calculate_sha256_for_file(file_path)
            filename = path_obj.name

            file_info = FileInfo(
                url=uri,
                checksum=checksum,
                filename=filename,
                file_size=file_size,
                modified_at=modified_at,
            )
            file_infos.append(file_info)

        file_infos.sort(key=lambda f: (f.modified_at is not None, f.modified_at), reverse=True)
        return file_infos

    def get_downloaded_file_signature_aliases(self, file_info: FileInfo) -> list[DownloadedFileSignature]:
        if not file_info.url.startswith("file://"):
            return []
        snapshot_root = (Path(self.artifact_path) / DOWNLOAD_FILE_PREFIX / settings.ENV).resolve()
        try:
            parts = Path(parse_uri_to_path(file_info.url)).resolve().relative_to(snapshot_root).parts
        except ValueError:
            return []
        if len(parts) != 3 and not (len(parts) == 5 and parts[2] == "attempts" and parts[3].isdigit()):
            return []
        if parts[-1] != file_info.filename:
            return []
        live_uri = f"file://{Path(get_download_dir(run_id=parts[1])) / file_info.filename}"
        return [to_downloaded_file_signature(file_info.model_copy(update={"url": live_uri}))]

    async def get_downloaded_files(
        self, organization_id: str, run_id: str | None, attempt_started_at: datetime | None = None
    ) -> list[FileInfo]:
        download_dir = get_download_dir(run_id=run_id)
        artifacts_by_uri: dict[str, Artifact] = {}
        if run_id is not None:
            try:
                artifacts = await app.DATABASE.artifacts.list_artifacts_for_run_by_type(
                    run_id=run_id,
                    organization_id=organization_id,
                    artifact_type=ArtifactType.DOWNLOAD,
                )
                artifacts_by_uri = {artifact.uri: artifact for artifact in artifacts}
            except Exception:
                # Local storage remains usable before the Forge app is initialized and during a
                # transient database outage. The file's stat metadata is still useful to the
                # attempt filter, and an un-attributed file must be handled fail-open there.
                LOG.warning(
                    "Failed to load local download artifact attribution",
                    organization_id=organization_id,
                    run_id=run_id,
                    exc_info=True,
                )
        snapshot_root = (
            Path(self.artifact_path) / DOWNLOAD_FILE_PREFIX / settings.ENV / organization_id / str(run_id)
        ).resolve()
        snapshots = {
            uri: artifact
            for uri, artifact in artifacts_by_uri.items()
            if uri.startswith("file://")
            and (snapshot_path := Path(parse_uri_to_path(uri))).is_relative_to(snapshot_root)
            and snapshot_path.is_file()
        }
        snapshot_contents = {
            (Path(parse_uri_to_path(artifact.uri)).name, artifact.checksum) for artifact in snapshots.values()
        }
        file_infos = [
            FileInfo(
                url=artifact.uri,
                checksum=artifact.checksum,
                filename=Path(parse_uri_to_path(artifact.uri)).name,
                file_size=artifact.file_size,
                modified_at=artifact.modified_at or artifact.created_at,
                artifact_id=artifact.artifact_id,
            )
            for artifact in snapshots.values()
        ]
        files_and_folders = os.listdir(download_dir)
        for file_or_folder in files_and_folders:
            path = os.path.join(download_dir, file_or_folder)
            if os.path.isfile(path):
                checksum = calculate_sha256_for_file(path)
                if (file_or_folder, checksum) in snapshot_contents:
                    continue
                uri = f"file://{path}"
                artifact = artifacts_by_uri.get(uri)
                modified_at: datetime | None
                try:
                    modified_at = datetime.fromtimestamp(os.stat(path).st_mtime, tz=UTC)
                except OSError:
                    LOG.warning("Failed to get local downloaded file modification time", path=path, exc_info=True)
                    modified_at = artifact.modified_at if artifact is not None else None
                try:
                    file_size = os.path.getsize(path)
                except OSError:
                    LOG.warning("Failed to get local downloaded file size", path=path, exc_info=True)
                    file_size = None
                file_info = FileInfo(
                    url=uri,
                    checksum=checksum,
                    filename=file_or_folder,
                    file_size=file_size,
                    modified_at=modified_at,
                    artifact_id=artifact.artifact_id if artifact is not None else None,
                )
                file_infos.append(file_info)
        return file_infos

    @staticmethod
    def _create_directories_if_not_exists(path_including_file_name: Path) -> None:
        path = path_including_file_name.parent
        path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _resolve_browser_storage_path(*relative_parts: str) -> Path | None:
        if not relative_parts:
            return None
        normalized_parts: list[str] = []
        for part in relative_parts:
            if part in {"", "."}:
                return None
            part_path = Path(part)
            if part_path.is_absolute() or part_path.drive:
                return None
            if any(segment in {"", ".", ".."} for segment in part_path.parts):
                return None
            normalized_parts.extend(part_path.parts)
        if not normalized_parts:
            return None
        base_path = Path(settings.BROWSER_SESSION_BASE_PATH).resolve()
        candidate = base_path.joinpath(*normalized_parts).resolve()
        try:
            candidate.relative_to(base_path)
        except ValueError:
            return None
        return candidate

    async def save_legacy_file(
        self, *, organization_id: str, filename: str, fileObj: BinaryIO
    ) -> tuple[str, str] | None:
        """Write an uploaded file under the same org-scoped layout the cloud backends use.

        ``organization_id`` is auth-derived and ``filename`` is reduced to its basename, so the
        destination cannot leave this organization's directory. The returned pair is
        (download URL, storage URI); local has no presigned URL, so both are the same ``file://``
        URI, which the read and delete paths re-check with ``assert_managed_file_access``. That URI
        names a path on the server, so it is not something a browser can fetch: a client's handle on
        the file is its id.
        """
        todays_date = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        # Resolved up front: as_uri rejects a relative path, and a relative ARTIFACT_STORAGE_PATH
        # would otherwise fail only after the bytes were written.
        directory = (Path(self.artifact_path) / settings.ENV / organization_id / todays_date).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        file_path = directory / bounded_basename(_windows_safe_filename(os.path.basename(filename)))
        fileObj.seek(0)
        async with aiofiles.open(file_path, "wb") as f:
            await f.write(fileObj.read())
        uri = managed_file_uri(file_path)
        return uri, uri

    async def delete_legacy_file(self, *, organization_id: str, uri: str) -> None:
        self.assert_managed_file_access(uri, organization_id)
        Path(parse_uri_to_path(uri)).unlink(missing_ok=True)

    def _build_browser_session_path(
        self,
        organization_id: str,
        browser_session_id: str,
        artifact_type: str,
        remote_path: str,
        date: str | None = None,
    ) -> Path:
        """Build the local path for a browser session file."""
        base = (
            Path(self.artifact_path)
            / settings.ENV
            / organization_id
            / "browser_sessions"
            / browser_session_id
            / artifact_type
        )
        if date:
            return base / date / remote_path
        return base / remote_path

    async def sync_browser_session_file(
        self,
        organization_id: str,
        browser_session_id: str,
        artifact_type: str,
        local_file_path: str,
        remote_path: str,
        date: str | None = None,
        recording_finalized_at: datetime | None = None,
        producer_run_id: str | None = None,
    ) -> str:
        """Sync a file from local browser session to local storage."""
        target_path = self._build_browser_session_path(
            organization_id, browser_session_id, artifact_type, remote_path, date
        )
        if WINDOWS:
            target_path = target_path.with_name(_windows_safe_filename(target_path.name))
        self._create_directories_if_not_exists(target_path)
        shutil.copy2(local_file_path, target_path)
        return f"file://{target_path}"

    async def delete_browser_session_file(
        self,
        organization_id: str,
        browser_session_id: str,
        artifact_type: str,
        remote_path: str,
        date: str | None = None,
    ) -> None:
        """Delete a file from browser session storage in local filesystem."""
        target_path = self._build_browser_session_path(
            organization_id, browser_session_id, artifact_type, remote_path, date
        )
        try:
            if target_path.exists():
                target_path.unlink()
        except Exception:
            LOG.exception("Failed to delete local browser session file", path=str(target_path))

    async def browser_session_file_exists(
        self,
        organization_id: str,
        browser_session_id: str,
        artifact_type: str,
        remote_path: str,
        date: str | None = None,
    ) -> bool:
        """Check if a file exists in browser session storage in local filesystem."""
        target_path = self._build_browser_session_path(
            organization_id, browser_session_id, artifact_type, remote_path, date
        )
        return target_path.exists()

    def manages_local_file_uri(self, uri: str, organization_id: str) -> bool:
        try:
            self.assert_managed_file_access(uri, organization_id)
        except PermissionError:
            return False
        return True

    def assert_managed_file_access(self, uri: str, organization_id: str) -> None:
        if not uri.startswith("file://"):
            raise PermissionError(f"No permission to access storage URI: {uri}")

        try:
            file_path = Path(parse_uri_to_path(uri)).resolve()
        except Exception as e:
            raise PermissionError(f"No permission to access storage URI: {uri}") from e

        allowed_dirs = (
            (Path(self.artifact_path) / organization_id).resolve(),
            (Path(self.artifact_path) / settings.ENV / organization_id).resolve(),
            (Path(self.artifact_path) / DOWNLOAD_FILE_PREFIX / settings.ENV / organization_id).resolve(),
        )
        if not any(
            os.path.commonpath([str(file_path), str(allowed_dir)]) == str(allowed_dir) for allowed_dir in allowed_dirs
        ):
            raise PermissionError(f"No permission to access storage URI: {uri}")

    async def download_managed_file(self, uri: str, organization_id: str) -> bytes | None:
        """Download a managed org-scoped file from local filesystem."""
        self.assert_managed_file_access(uri, organization_id)

        try:
            file_path = parse_uri_to_path(uri)
            async with aiofiles.open(file_path, "rb") as f:
                return await f.read()
        except Exception:
            LOG.exception("Failed to read local file", uri=uri)
            return None

    async def file_exists(self, uri: str) -> bool:
        """Check if a file exists at the given local URI."""
        try:
            file_path = parse_uri_to_path(uri)
            return os.path.exists(file_path)
        except Exception:
            return False

    @property
    def storage_type(self) -> str:
        """Returns 'file' as the storage type."""
        return "file"
