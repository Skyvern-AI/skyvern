from pydantic import BaseModel, Field


class FileInfo(BaseModel):
    """Information about a downloaded file, including URL and checksum."""

    url: str = Field(..., description="URL to access the file")
    checksum: str | None = Field(None, description="SHA-256 checksum of the file")
    filename: str | None = Field(None, description="Original filename")
