from abc import ABC, abstractmethod

from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.models import Step

# TODO: This should be a part of the ArtifactType model
FILE_EXTENTSION_MAP: dict[ArtifactType, str] = {
    ArtifactType.RECORDING: "webm",
    ArtifactType.SCREENSHOT_LLM: "png",
    ArtifactType.SCREENSHOT_ACTION: "png",
    ArtifactType.SCREENSHOT_FINAL: "png",
    ArtifactType.LLM_PROMPT: "txt",
    ArtifactType.LLM_REQUEST: "json",
    ArtifactType.LLM_RESPONSE: "json",
    ArtifactType.LLM_RESPONSE_PARSED: "json",
    ArtifactType.VISIBLE_ELEMENTS_ID_XPATH_MAP: "json",
    ArtifactType.VISIBLE_ELEMENTS_ID_FRAME_MAP: "json",
    ArtifactType.VISIBLE_ELEMENTS_TREE: "json",
    ArtifactType.VISIBLE_ELEMENTS_TREE_TRIMMED: "json",
    ArtifactType.VISIBLE_ELEMENTS_TREE_IN_PROMPT: "txt",
    ArtifactType.HTML_SCRAPE: "html",
    ArtifactType.HTML_ACTION: "html",
    ArtifactType.TRACE: "zip",
    ArtifactType.HAR: "har",
}


class BaseStorage(ABC):
    @abstractmethod
    def build_uri(self, artifact_id: str, step: Step, artifact_type: ArtifactType) -> str:
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
