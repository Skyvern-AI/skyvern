from datetime import datetime

from pydantic import BaseModel, Field


class FileInfo(BaseModel):
    """Information about a downloaded file, including URL and checksum."""

    url: str = Field(..., description="URL to access the file")
    checksum: str | None = Field(None, description="SHA-256 checksum of the file")
    filename: str | None = Field(None, description="Original filename")
    modified_at: datetime | None = Field(None, description="Modified time of the file")
    # Optional: when the FileInfo is built from a DOWNLOAD Artifact row, the
    # row's id is carried through so persisted snapshots (e.g. block outputs)
    # can rebuild fresh signed URLs at API-fetch time even if the snapshot's
    # ``url`` was minted before the artifact-first read existed.
    artifact_id: str | None = Field(None, description="Artifact row id for refresh-on-read")
