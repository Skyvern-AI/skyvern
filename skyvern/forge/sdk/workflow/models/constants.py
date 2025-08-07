from enum import StrEnum


class FileStorageType(StrEnum):
    S3 = "s3"
    AZURE = "azure"