from enum import StrEnum
from typing import Any, Callable
from urllib.parse import urlparse

import aioboto3
import structlog
from aiobotocore.client import AioBaseClient

from skyvern.forge.sdk.settings_manager import SettingsManager

LOG = structlog.get_logger()


class AWSClientType(StrEnum):
    S3 = "s3"
    SECRETS_MANAGER = "secretsmanager"


def execute_with_async_client(client_type: AWSClientType) -> Callable:
    def decorator(f: Callable) -> Callable:
        async def wrapper(*args: list[Any], **kwargs: dict[str, Any]) -> Any:
            self = args[0]
            assert isinstance(self, AsyncAWSClient)
            session = aioboto3.Session()
            async with session.client(client_type, region_name=SettingsManager.get_settings().AWS_REGION) as client:
                return await f(*args, client=client, **kwargs)

        return wrapper

    return decorator


class AsyncAWSClient:
    @execute_with_async_client(client_type=AWSClientType.SECRETS_MANAGER)
    async def get_secret(self, secret_name: str, client: AioBaseClient = None) -> str | None:
        try:
            response = await client.get_secret_value(SecretId=secret_name)
            return response["SecretString"]
        except Exception as e:
            try:
                error_code = e.response["Error"]["Code"]  # type: ignore
            except Exception:
                error_code = "failed-to-get-error-code"
            LOG.exception("Failed to get secret.", secret_name=secret_name, error_code=error_code)
            return None

    @execute_with_async_client(client_type=AWSClientType.S3)
    async def upload_file(self, uri: str, data: bytes, client: AioBaseClient = None) -> str | None:
        try:
            parsed_uri = S3Uri(uri)
            await client.put_object(Body=data, Bucket=parsed_uri.bucket, Key=parsed_uri.key)
            LOG.debug("Upload file success", uri=uri)
            return uri
        except Exception:
            LOG.exception("S3 upload failed.", uri=uri)
            return None

    @execute_with_async_client(client_type=AWSClientType.S3)
    async def upload_file_from_path(self, uri: str, file_path: str, client: AioBaseClient = None) -> None:
        try:
            parsed_uri = S3Uri(uri)
            await client.upload_file(file_path, parsed_uri.bucket, parsed_uri.key)
            LOG.info("Upload file from path success", uri=uri)
        except Exception:
            LOG.exception("S3 upload failed.", uri=uri)

    @execute_with_async_client(client_type=AWSClientType.S3)
    async def download_file(self, uri: str, client: AioBaseClient = None, log_exception: bool = True) -> bytes | None:
        try:
            parsed_uri = S3Uri(uri)
            response = await client.get_object(Bucket=parsed_uri.bucket, Key=parsed_uri.key)
            return await response["Body"].read()
        except Exception:
            if log_exception:
                LOG.exception("S3 download failed", uri=uri)
            return None

    @execute_with_async_client(client_type=AWSClientType.S3)
    async def create_presigned_urls(self, uris: list[str], client: AioBaseClient = None) -> list[str] | None:
        presigned_urls = []
        try:
            for uri in uris:
                parsed_uri = S3Uri(uri)
                url = await client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": parsed_uri.bucket, "Key": parsed_uri.key},
                    ExpiresIn=SettingsManager.get_settings().PRESIGNED_URL_EXPIRATION,
                )
                presigned_urls.append(url)

            return presigned_urls
        except Exception:
            LOG.exception("Failed to create presigned url for S3 objects.", uris=uris)
            return None


class S3Uri(object):
    # From: https://stackoverflow.com/questions/42641315/s3-urls-get-bucket-name-and-path
    """
    >>> s = S3Uri("s3://bucket/hello/world")
    >>> s.bucket
    'bucket'
    >>> s.key
    'hello/world'
    >>> s.uri
    's3://bucket/hello/world'

    >>> s = S3Uri("s3://bucket/hello/world?qwe1=3#ddd")
    >>> s.bucket
    'bucket'
    >>> s.key
    'hello/world?qwe1=3#ddd'
    >>> s.uri
    's3://bucket/hello/world?qwe1=3#ddd'

    >>> s = S3Uri("s3://bucket/hello/world#foo?bar=2")
    >>> s.key
    'hello/world#foo?bar=2'
    >>> s.uri
    's3://bucket/hello/world#foo?bar=2'
    """

    def __init__(self, uri: str) -> None:
        self._parsed = urlparse(uri, allow_fragments=False)

    @property
    def bucket(self) -> str:
        return self._parsed.netloc

    @property
    def key(self) -> str:
        if self._parsed.query:
            return self._parsed.path.lstrip("/") + "?" + self._parsed.query
        else:
            return self._parsed.path.lstrip("/")

    @property
    def uri(self) -> str:
        return self._parsed.geturl()
