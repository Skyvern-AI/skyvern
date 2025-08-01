import base64
import hashlib
from datetime import datetime

import structlog

from skyvern.forge import app
from skyvern.schemas.projects import FileNode, ProjectFile

LOG = structlog.get_logger(__name__)


async def build_file_tree(
    files: list[ProjectFile],
    organization_id: str,
    project_id: str,
    project_version: int,
    project_revision_id: str,
) -> dict[str, FileNode]:
    """Build a hierarchical file tree from a list of files and upload the files to s3 with the same tree structure."""
    file_tree: dict[str, FileNode] = {}

    for file in files:
        # Decode content to calculate size and hash
        content_bytes = base64.b64decode(file.content)
        content_hash = hashlib.sha256(content_bytes).hexdigest()
        file_size = len(content_bytes)

        # Create artifact and upload to S3
        try:
            artifact_id = await app.ARTIFACT_MANAGER.create_project_file_artifact(
                organization_id=organization_id,
                project_id=project_id,
                project_version=project_version,
                file_path=file.path,
                data=content_bytes,
            )
            LOG.debug(
                "Created project file artifact",
                artifact_id=artifact_id,
                file_path=file.path,
                project_id=project_id,
                project_version=project_version,
            )
            # create a project file record
            await app.DATABASE.create_project_file(
                project_revision_id=project_revision_id,
                project_id=project_id,
                organization_id=organization_id,
                file_path=file.path,
                file_name=file.path.split("/")[-1],
                file_type="file",
                content_hash=f"sha256:{content_hash}",
                file_size=file_size,
                mime_type=file.mime_type,
                artifact_id=artifact_id,
            )
        except Exception:
            LOG.exception(
                "Failed to create project file artifact",
                file_path=file.path,
                project_id=project_id,
                project_version=project_version,
                project_revision_id=project_revision_id,
            )
            raise

        # Split path into components
        path_parts = file.path.split("/")
        current_level = file_tree

        # Create directory structure
        for _, part in enumerate(path_parts[:-1]):
            if part not in current_level:
                current_level[part] = FileNode(type="directory", created_at=datetime.utcnow(), children={})
            elif current_level[part].type == "file":
                # Convert file to directory if needed
                current_level[part] = FileNode(type="directory", created_at=current_level[part].created_at, children={})

            current_level = current_level[part].children or {}

        # Add the file
        filename = path_parts[-1]
        current_level[filename] = FileNode(
            type="file",
            size=file_size,
            mime_type=file.mime_type,
            content_hash=f"sha256:{content_hash}",
            created_at=datetime.utcnow(),
        )

    return file_tree
