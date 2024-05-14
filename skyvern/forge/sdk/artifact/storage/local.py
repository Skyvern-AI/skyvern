from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import structlog

from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.artifact.storage.base import FILE_EXTENTSION_MAP, BaseStorage
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.settings_manager import SettingsManager

LOG = structlog.get_logger()


class LocalStorage(BaseStorage):
    def __init__(self, artifact_path: str = SettingsManager.get_settings().ARTIFACT_STORAGE_PATH) -> None:
        self.artifact_path = artifact_path

    def build_uri(self, artifact_id: str, step: Step, artifact_type: ArtifactType) -> str:
        file_ext = FILE_EXTENTSION_MAP[artifact_type]
        return f"file://{self.artifact_path}/{step.task_id}/{step.order:02d}_{step.retry_index}_{step.step_id}/{datetime.utcnow().isoformat()}_{artifact_id}_{artifact_type}.{file_ext}"

    async def store_artifact(self, artifact: Artifact, data: bytes) -> None:
        file_path = None
        try:
            file_path = Path(self._parse_uri_to_path(artifact.uri))
            self._create_directories_if_not_exists(file_path)
            with open(file_path, "wb") as f:
                f.write(data)
        except Exception:
            LOG.exception("Failed to store artifact locally.", file_path=file_path, artifact=artifact)

    async def store_artifact_from_path(self, artifact: Artifact, path: str) -> None:
        file_path = None
        try:
            file_path = Path(self._parse_uri_to_path(artifact.uri))
            self._create_directories_if_not_exists(file_path)
            Path(path).replace(file_path)
        except Exception:
            LOG.exception("Failed to store artifact locally.", file_path=file_path, artifact=artifact)

    async def retrieve_artifact(self, artifact: Artifact) -> bytes | None:
        file_path = None
        try:
            file_path = self._parse_uri_to_path(artifact.uri)
            with open(file_path, "rb") as f:
                return f.read()
        except Exception:
            LOG.exception("Failed to retrieve local artifact.", file_path=file_path, artifact=artifact)
            return None

    async def get_share_link(self, artifact: Artifact) -> str:
        return artifact.uri

    async def get_share_links(self, artifacts: list[Artifact]) -> list[str]:
        return [artifact.uri for artifact in artifacts]

    @staticmethod
    def _parse_uri_to_path(uri: str) -> str:
        parsed_uri = urlparse(uri)
        if parsed_uri.scheme != "file":
            raise ValueError("Invalid URI scheme: {parsed_uri.scheme} expected: file")
        path = parsed_uri.netloc + parsed_uri.path
        return unquote(path)

    @staticmethod
    def _create_directories_if_not_exists(path_including_file_name: Path) -> None:
        path = path_including_file_name.parent
        path.mkdir(parents=True, exist_ok=True)
