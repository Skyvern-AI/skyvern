import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

import structlog

from skyvern.config import settings
from skyvern.forge.sdk.api.files import (
    calculate_sha256_for_file,
    get_download_dir,
    get_skyvern_temp_dir,
    parse_uri_to_path,
)
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType, LogEntityType
from skyvern.forge.sdk.artifact.storage.base import FILE_EXTENTSION_MAP, BaseStorage
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.ai_suggestions import AISuggestion
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.task_v2 import TaskV2, Thought
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock

LOG = structlog.get_logger()
WINDOWS = os.name == "nt"


def _safe_timestamp() -> str:
    ts = datetime.utcnow().isoformat()
    return ts.replace(":", "-") if WINDOWS else ts


def _windows_safe_filename(name: str) -> str:
    if not WINDOWS:
        return name
    invalid = '<>:"/\\|?*'
    name = "".join("-" if ch in invalid else ch for ch in name)
    return name.rstrip(" .")


class LocalStorage(BaseStorage):
    def __init__(self, artifact_path: str = settings.ARTIFACT_STORAGE_PATH) -> None:
        self.artifact_path = artifact_path

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
            with open(file_path) as f:
                return [line.strip() for line in f.readlines() if line.strip()]
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
        return f"file://{self.artifact_path}/{settings.ENV}/{organization_id}/scripts/{script_id}/{script_version}/{file_path}"

    async def store_artifact(self, artifact: Artifact, data: bytes) -> None:
        file_path = None
        try:
            file_path = Path(parse_uri_to_path(artifact.uri))
            if WINDOWS:
                file_path = file_path.with_name(_windows_safe_filename(file_path.name))
            self._create_directories_if_not_exists(file_path)
            with open(file_path, "wb") as f:
                f.write(data)
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
            with open(file_path, "rb") as f:
                return f.read()
        except Exception:
            LOG.exception(
                "Failed to retrieve local artifact.",
                file_path=file_path,
                artifact=artifact,
            )
            return None

    async def get_share_link(self, artifact: Artifact) -> str | None:
        return None

    async def get_share_links(self, artifacts: list[Artifact]) -> list[str] | None:
        return None

    async def save_streaming_file(self, organization_id: str, file_name: str) -> None:
        return

    async def get_streaming_file(self, organization_id: str, file_name: str) -> bytes | None:
        # make the directory if it doesn't exist
        Path(f"{get_skyvern_temp_dir()}/{organization_id}").mkdir(parents=True, exist_ok=True)
        file_path = Path(f"{get_skyvern_temp_dir()}/{organization_id}/{file_name}")
        try:
            with open(file_path, "rb") as f:
                return f.read()
        except Exception:
            return None

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

        # Copy all files from the directory to the stored folder
        for root, _, files in os.walk(source_directory):
            for file in files:
                source_file_path = Path(root) / file
                relative_path = source_file_path.relative_to(source_directory)
                target_file_path = stored_folder_path / relative_path
                self._create_directories_if_not_exists(target_file_path)
                shutil.copy2(source_file_path, target_file_path)

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
        self._create_directories_if_not_exists(stored_folder_path)
        LOG.info(
            "Storing browser profile locally",
            organization_id=organization_id,
            profile_id=profile_id,
            directory=str(source_directory),
            browser_profile_path=str(stored_folder_path),
        )

        for root, _, files in os.walk(source_directory):
            for file in files:
                source_file_path = Path(root) / file
                relative_path = source_file_path.relative_to(source_directory)
                target_file_path = stored_folder_path / relative_path
                self._create_directories_if_not_exists(target_file_path)
                shutil.copy2(source_file_path, target_file_path)

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

    async def save_downloaded_files(self, organization_id: str, run_id: str | None) -> None:
        pass

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

    async def list_recordings_in_browser_session(self, organization_id: str, browser_session_id: str) -> list[str]:
        """List all recording files for a browser session (not implemented for local storage)."""
        return []

    async def get_shared_recordings_in_browser_session(
        self, organization_id: str, browser_session_id: str
    ) -> list[FileInfo]:
        """Get recording files with URLs for a browser session (not implemented for local storage)."""
        return []

    async def get_downloaded_files(self, organization_id: str, run_id: str | None) -> list[FileInfo]:
        download_dir = get_download_dir(run_id=run_id)
        file_infos: list[FileInfo] = []
        files_and_folders = os.listdir(download_dir)
        for file_or_folder in files_and_folders:
            path = os.path.join(download_dir, file_or_folder)
            if os.path.isfile(path):
                # Calculate checksum for the file
                checksum = calculate_sha256_for_file(path)
                file_info = FileInfo(url=f"file://{path}", checksum=checksum, filename=file_or_folder)
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
        raise NotImplementedError(
            "Legacy file storage is not implemented for LocalStorage. Please use a different storage backend."
        )

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

    async def download_uploaded_file(self, uri: str) -> bytes | None:
        """Download a user-uploaded file from local filesystem."""
        try:
            file_path = parse_uri_to_path(uri)
            with open(file_path, "rb") as f:
                return f.read()
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
