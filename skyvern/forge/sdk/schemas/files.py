from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class FileInfo(BaseModel):
    """Information about a downloaded file, including URL and checksum."""

    url: str = Field(..., description="URL to access the file")
    checksum: str | None = Field(None, description="SHA-256 checksum of the file")
    filename: str | None = Field(None, description="Original filename")
    file_size: int | None = Field(None, description="Size of the file in bytes")
    modified_at: datetime | None = Field(None, description="Modified time of the file")
    # Optional: when the FileInfo is built from a DOWNLOAD Artifact row, the
    # row's id is carried through so persisted snapshots (e.g. block outputs)
    # can rebuild fresh signed URLs at API-fetch time even if the snapshot's
    # ``url`` was minted before the artifact-first read existed.
    artifact_id: str | None = Field(None, description="Artifact row id for refresh-on-read")


class UploadedFile(BaseModel):
    """A file uploaded through ``POST /v1/upload_file``.

    ``storage_uri`` is the server's own record of where the bytes live and is the only
    value the delete and purge paths dereference; it is never populated from caller input.
    """

    model_config = ConfigDict(from_attributes=True)

    file_id: str
    organization_id: str
    storage_uri: str
    filename: str
    size_bytes: int | None = None
    expires_at: datetime | None = None
    deleted_at: datetime | None = None
    created_at: datetime
    modified_at: datetime
