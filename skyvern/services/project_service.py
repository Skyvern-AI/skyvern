import base64
import hashlib
import os
import subprocess
from datetime import datetime

import structlog
from fastapi import BackgroundTasks

from skyvern.exceptions import ProjectNotFound
from skyvern.forge import app
from skyvern.schemas.projects import FileNode, ProjectFileCreate

LOG = structlog.get_logger(__name__)


async def build_file_tree(
    files: list[ProjectFileCreate],
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


async def execute_project(
    project_id: str,
    organization_id: str,
    background_tasks: BackgroundTasks | None = None,
) -> None:
    # TODO: assume the project only has one ProjectFile called main.py
    # step 1: get the project revision
    # step 2: get the project files
    # step 3: copy the project files to the local directory
    # step 4: execute the project

    # step 1: get the project revision
    project = await app.DATABASE.get_project(
        project_id=project_id,
        organization_id=organization_id,
    )
    if not project:
        raise ProjectNotFound(project_id=project_id)

    # step 2: get the project files
    project_files = await app.DATABASE.get_project_files(
        project_revision_id=project.project_revision_id, organization_id=organization_id
    )

    # step 3: copy the project files to the local directory
    for file in project_files:
        # retrieve the artifact
        if not file.artifact_id:
            continue
        artifact = await app.DATABASE.get_artifact_by_id(file.artifact_id, organization_id)
        if not artifact:
            LOG.error("Artifact not found", artifact_id=file.artifact_id, project_id=project_id)
            continue
        file_content = await app.ARTIFACT_MANAGER.retrieve_artifact(artifact)
        if not file_content:
            continue
        file_path = os.path.join(project.project_id, file.file_path)
        # create the directory if it doesn't exist
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        # Determine the encoding to use
        encoding = "utf-8"

        try:
            # Try to decode as text
            if file.mime_type and file.mime_type.startswith("text/"):
                # Text file - decode as string
                with open(file_path, "w", encoding=encoding) as f:
                    f.write(file_content.decode(encoding))
            else:
                # Binary file - write as bytes
                with open(file_path, "wb") as f:
                    f.write(file_content)
        except UnicodeDecodeError:
            # Fallback to binary mode if text decoding fails
            with open(file_path, "wb") as f:
                f.write(file_content)

    # step 4: execute the project
    if background_tasks:
        background_tasks.add_task(subprocess.run, ["python", f"{project.project_id}/main.py"])
    LOG.info("Project executed successfully", project_id=project_id)
