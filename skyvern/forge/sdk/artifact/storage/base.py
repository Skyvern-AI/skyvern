from abc import ABC, abstractmethod
from typing import BinaryIO

from skyvern.forge import app
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType, LogEntityType
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.ai_suggestions import AISuggestion
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.task_v2 import TaskV2, Thought
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock


async def _file_infos_from_artifacts(artifacts: list[Artifact], *, artifact_type: ArtifactType) -> list[FileInfo]:
    """Build the API-shaped ``FileInfo`` list from a homogeneous batch of
    artifact rows (e.g. all DOWNLOAD or all RECORDING).

    Filename is the URI basename (the save site writes ``{base_uri}/{file}``);
    checksum and modified_at come straight from the row, so retrieval needs
    zero S3 round-trips.

    All artifacts in a single batch share the same organization (downloads /
    recordings are scoped to a run or browser session, which is scoped to an
    org), so the per-org URL TTL is resolved once and applied to every URL.

    The ``artifact_type`` is only used for the URL's informational query
    parameter — it does not affect the HMAC signature. Callers must pass rows
    of a single type so the URL hint is correct.
    """
    if not artifacts:
        return []
    organization_id = artifacts[0].organization_id
    expiry_seconds = await app.ARTIFACT_MANAGER.resolve_artifact_url_expiry_seconds(organization_id)
    infos: list[FileInfo] = []
    for artifact in artifacts:
        filename = artifact.uri.rsplit("/", 1)[-1] if artifact.uri else ""
        url = app.ARTIFACT_MANAGER.build_signed_content_url(
            artifact_id=artifact.artifact_id,
            artifact_name=filename,
            artifact_type=artifact_type.value,
            expiry_seconds=expiry_seconds,
        )
        infos.append(
            FileInfo(
                url=url,
                checksum=artifact.checksum,
                filename=filename,
                modified_at=artifact.created_at,
                artifact_id=artifact.artifact_id,
            )
        )
    return infos


async def _file_infos_from_download_artifacts(artifacts: list[Artifact]) -> list[FileInfo]:
    """Backward-compat alias for DOWNLOAD-typed callers.

    Forwards to :func:`_file_infos_from_artifacts` with the DOWNLOAD type so
    pre-existing import sites keep working without each having to thread the
    artifact_type through.
    """
    return await _file_infos_from_artifacts(artifacts, artifact_type=ArtifactType.DOWNLOAD)


# TODO: This should be a part of the ArtifactType model
FILE_EXTENTSION_MAP: dict[ArtifactType, str] = {
    ArtifactType.RECORDING: "webm",
    ArtifactType.BROWSER_CONSOLE_LOG: "log",
    ArtifactType.SCREENSHOT_LLM: "png",
    ArtifactType.SCREENSHOT_ACTION: "png",
    ArtifactType.SCREENSHOT_FINAL: "png",
    ArtifactType.SKYVERN_LOG: "log",
    ArtifactType.SKYVERN_LOG_RAW: "json",
    ArtifactType.LLM_PROMPT: "txt",
    ArtifactType.LLM_REQUEST: "json",
    ArtifactType.LLM_RESPONSE: "json",
    ArtifactType.LLM_RESPONSE_PARSED: "json",
    ArtifactType.LLM_RESPONSE_RENDERED: "json",
    ArtifactType.VISIBLE_ELEMENTS_ID_CSS_MAP: "json",
    ArtifactType.VISIBLE_ELEMENTS_ID_FRAME_MAP: "json",
    ArtifactType.VISIBLE_ELEMENTS_TREE: "json",
    ArtifactType.VISIBLE_ELEMENTS_TREE_TRIMMED: "json",
    ArtifactType.VISIBLE_ELEMENTS_TREE_IN_PROMPT: "txt",
    ArtifactType.HTML_SCRAPE: "html",
    ArtifactType.HTML_ACTION: "html",
    ArtifactType.TRACE: "zip",
    ArtifactType.HAR: "har",
    ArtifactType.HASHED_HREF_MAP: "json",
    # DEPRECATED: we're using CSS selector map now
    ArtifactType.VISIBLE_ELEMENTS_ID_XPATH_MAP: "json",
    ArtifactType.PDF: "pdf",
    ArtifactType.STEP_ARCHIVE: "zip",
    ArtifactType.TASK_ARCHIVE: "zip",
}


class BaseStorage(ABC):
    @abstractmethod
    def build_uri(self, *, organization_id: str, artifact_id: str, step: Step, artifact_type: ArtifactType) -> str:
        pass

    @abstractmethod
    async def retrieve_global_workflows(self) -> list[str]:
        pass

    @abstractmethod
    def build_log_uri(
        self, *, organization_id: str, log_entity_type: LogEntityType, log_entity_id: str, artifact_type: ArtifactType
    ) -> str:
        pass

    @abstractmethod
    def build_thought_uri(
        self, *, organization_id: str, artifact_id: str, thought: Thought, artifact_type: ArtifactType
    ) -> str:
        pass

    @abstractmethod
    def build_task_v2_uri(
        self, *, organization_id: str, artifact_id: str, task_v2: TaskV2, artifact_type: ArtifactType
    ) -> str:
        pass

    @abstractmethod
    def build_workflow_run_block_uri(
        self,
        *,
        organization_id: str,
        artifact_id: str,
        workflow_run_block: WorkflowRunBlock,
        artifact_type: ArtifactType,
    ) -> str:
        pass

    @abstractmethod
    def build_ai_suggestion_uri(
        self, *, organization_id: str, artifact_id: str, ai_suggestion: AISuggestion, artifact_type: ArtifactType
    ) -> str:
        pass

    @abstractmethod
    def build_script_file_uri(
        self, *, organization_id: str, script_id: str, script_version: int, file_path: str
    ) -> str:
        pass

    @abstractmethod
    async def store_artifact(self, artifact: Artifact, data: bytes) -> None:
        pass

    @abstractmethod
    async def retrieve_artifact(self, artifact: Artifact) -> bytes | None:
        pass

    @abstractmethod
    async def get_share_link(self, artifact: Artifact) -> str | None:
        pass

    @abstractmethod
    async def get_share_links(self, artifacts: list[Artifact]) -> list[str] | None:
        pass

    @abstractmethod
    async def store_artifact_from_path(self, artifact: Artifact, path: str) -> None:
        pass

    @abstractmethod
    async def save_streaming_file(self, organization_id: str, file_name: str) -> None:
        pass

    @abstractmethod
    async def get_streaming_file(self, organization_id: str, file_name: str) -> bytes | None:
        pass

    @abstractmethod
    async def store_browser_session(self, organization_id: str, workflow_permanent_id: str, directory: str) -> None:
        pass

    @abstractmethod
    async def retrieve_browser_session(self, organization_id: str, workflow_permanent_id: str) -> str | None:
        pass

    @abstractmethod
    async def delete_browser_session(self, organization_id: str, workflow_permanent_id: str) -> None:
        pass

    @abstractmethod
    async def store_browser_profile(self, organization_id: str, profile_id: str, directory: str) -> None:
        """Store a browser profile from a directory."""

    @abstractmethod
    async def retrieve_browser_profile(self, organization_id: str, profile_id: str) -> str | None:
        """Retrieve a browser profile to a temporary directory."""

    @abstractmethod
    async def list_downloaded_files_in_browser_session(
        self, organization_id: str, browser_session_id: str
    ) -> list[str]:
        pass

    @abstractmethod
    async def list_downloading_files_in_browser_session(
        self, organization_id: str, browser_session_id: str
    ) -> list[str]:
        pass

    @abstractmethod
    async def get_shared_downloaded_files_in_browser_session(
        self, organization_id: str, browser_session_id: str
    ) -> list[FileInfo]:
        pass

    @abstractmethod
    async def get_shared_recordings_in_browser_session(
        self, organization_id: str, browser_session_id: str
    ) -> list[FileInfo]:
        pass

    @abstractmethod
    async def save_downloaded_files(
        self,
        organization_id: str,
        run_id: str | None,
    ) -> None:
        pass

    @abstractmethod
    async def get_downloaded_files(self, organization_id: str, run_id: str | None) -> list[FileInfo]:
        pass

    @abstractmethod
    async def save_legacy_file(
        self, *, organization_id: str, filename: str, fileObj: BinaryIO
    ) -> tuple[str, str] | None:
        pass

    @abstractmethod
    async def sync_browser_session_file(
        self,
        organization_id: str,
        browser_session_id: str,
        artifact_type: str,
        local_file_path: str,
        remote_path: str,
        date: str | None = None,
    ) -> str:
        pass

    @abstractmethod
    async def delete_browser_session_file(
        self,
        organization_id: str,
        browser_session_id: str,
        artifact_type: str,
        remote_path: str,
        date: str | None = None,
    ) -> None:
        pass

    @abstractmethod
    async def browser_session_file_exists(
        self,
        organization_id: str,
        browser_session_id: str,
        artifact_type: str,
        remote_path: str,
        date: str | None = None,
    ) -> bool:
        pass

    @abstractmethod
    def assert_managed_file_access(self, uri: str, organization_id: str) -> None:
        pass

    @abstractmethod
    async def download_managed_file(self, uri: str, organization_id: str) -> bytes | None:
        pass

    @abstractmethod
    async def file_exists(self, uri: str) -> bool:
        pass

    @property
    @abstractmethod
    def storage_type(self) -> str:
        pass
