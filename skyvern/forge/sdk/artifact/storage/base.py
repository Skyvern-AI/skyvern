from abc import ABC, abstractmethod

from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType, LogEntityType
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.observers import ObserverCruise, ObserverThought
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock

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
    ArtifactType.VISIBLE_ELEMENTS_ID_CSS_MAP: "json",
    ArtifactType.VISIBLE_ELEMENTS_ID_FRAME_MAP: "json",
    ArtifactType.VISIBLE_ELEMENTS_TREE: "json",
    ArtifactType.VISIBLE_ELEMENTS_TREE_TRIMMED: "json",
    ArtifactType.VISIBLE_ELEMENTS_TREE_IN_PROMPT: "txt",
    ArtifactType.HTML_SCRAPE: "html",
    ArtifactType.HTML_ACTION: "html",
    ArtifactType.TRACE: "zip",
    ArtifactType.HAR: "har",
    # DEPRECATED: we're using CSS selector map now
    ArtifactType.VISIBLE_ELEMENTS_ID_XPATH_MAP: "json",
}


class BaseStorage(ABC):
    @abstractmethod
    def build_uri(self, artifact_id: str, step: Step, artifact_type: ArtifactType) -> str:
        pass

    @abstractmethod
    def build_log_uri(self, log_entity_type: LogEntityType, log_entity_id: str, artifact_type: ArtifactType) -> str:
        pass

    @abstractmethod
    def build_observer_thought_uri(
        self, artifact_id: str, observer_thought: ObserverThought, artifact_type: ArtifactType
    ) -> str:
        pass

    @abstractmethod
    def build_observer_cruise_uri(
        self, artifact_id: str, observer_cruise: ObserverCruise, artifact_type: ArtifactType
    ) -> str:
        pass

    @abstractmethod
    def build_workflow_run_block_uri(
        self, artifact_id: str, workflow_run_block: WorkflowRunBlock, artifact_type: ArtifactType
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
    async def get_streaming_file(self, organization_id: str, file_name: str, use_default: bool = True) -> bytes | None:
        pass

    @abstractmethod
    async def store_browser_session(self, organization_id: str, workflow_permanent_id: str, directory: str) -> None:
        pass

    @abstractmethod
    async def retrieve_browser_session(self, organization_id: str, workflow_permanent_id: str) -> str | None:
        pass

    @abstractmethod
    async def save_downloaded_files(
        self, organization_id: str, task_id: str | None, workflow_run_id: str | None
    ) -> None:
        pass

    @abstractmethod
    async def get_downloaded_files(
        self, organization_id: str, task_id: str | None, workflow_run_id: str | None
    ) -> list[str]:
        pass
