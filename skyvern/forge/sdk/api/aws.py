from enum import StrEnum
from typing import IO, Any, Callable
from urllib.parse import urlparse

import aioboto3
import structlog
from aiobotocore.client import AioBaseClient

from skyvern.config import settings

LOG = structlog.get_logger()


class AWSClientType(StrEnum):
    S3 = "s3"
    SECRETS_MANAGER = "secretsmanager"
    ECS = "ecs"


def execute_with_async_client(client_type: AWSClientType) -> Callable:
    def decorator(f: Callable) -> Callable:
        async def wrapper(*args: list[Any], **kwargs: dict[str, Any]) -> Any:
            self = args[0]
            assert isinstance(self, AsyncAWSClient)
            session = aioboto3.Session()
            async with session.client(client_type, region_name=settings.AWS_REGION) as client:
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

    @execute_with_async_client(client_type=AWSClientType.SECRETS_MANAGER)
    async def create_secret(self, secret_name: str, secret_value: str, client: AioBaseClient = None) -> None:
        try:
            await client.create_secret(Name=secret_name, SecretString=secret_value)
        except Exception as e:
            LOG.exception("Failed to create secret.", secret_name=secret_name)
            raise e

    @execute_with_async_client(client_type=AWSClientType.SECRETS_MANAGER)
    async def set_secret(self, secret_name: str, secret_value: str, client: AioBaseClient = None) -> None:
        try:
            await client.put_secret_value(SecretId=secret_name, SecretString=secret_value)
        except Exception as e:
            LOG.exception("Failed to set secret.", secret_name=secret_name)
            raise e

    @execute_with_async_client(client_type=AWSClientType.SECRETS_MANAGER)
    async def delete_secret(self, secret_name: str, client: AioBaseClient = None) -> None:
        try:
            await client.delete_secret(SecretId=secret_name)
        except Exception as e:
            LOG.exception("Failed to delete secret.", secret_name=secret_name)
            raise e

    @execute_with_async_client(client_type=AWSClientType.S3)
    async def upload_file(self, uri: str, data: bytes, client: AioBaseClient = None) -> str | None:
        try:
            parsed_uri = S3Uri(uri)
            await client.put_object(Body=data, Bucket=parsed_uri.bucket, Key=parsed_uri.key)
            return uri
        except Exception:
            LOG.exception("S3 upload failed.", uri=uri)
            return None

    @execute_with_async_client(client_type=AWSClientType.S3)
    async def upload_file_stream(self, uri: str, file_obj: IO[bytes], client: AioBaseClient = None) -> str | None:
        try:
            parsed_uri = S3Uri(uri)
            await client.upload_fileobj(file_obj, parsed_uri.bucket, parsed_uri.key)
            LOG.debug("Upload file stream success", uri=uri)
            return uri
        except Exception:
            LOG.exception("S3 upload stream failed.", uri=uri)
            return None

    @execute_with_async_client(client_type=AWSClientType.S3)
    async def upload_file_from_path(
        self, uri: str, file_path: str, client: AioBaseClient = None, metadata: dict | None = None
    ) -> None:
        try:
            parsed_uri = S3Uri(uri)
            params: dict[str, Any] = {
                "Filename": file_path,
                "Bucket": parsed_uri.bucket,
                "Key": parsed_uri.key,
            }

            if metadata:
                params["ExtraArgs"] = {"Metadata": metadata}

            await client.upload_file(**params)
        except Exception:
            LOG.exception("S3 upload failed.", uri=uri)

    @execute_with_async_client(client_type=AWSClientType.S3)
    async def download_file(self, uri: str, client: AioBaseClient = None, log_exception: bool = True) -> bytes | None:
        try:
            parsed_uri = S3Uri(uri)

            # Get full object including body
            response = await client.get_object(Bucket=parsed_uri.bucket, Key=parsed_uri.key)
            return await response["Body"].read()
        except Exception:
            if log_exception:
                LOG.exception("S3 download failed", uri=uri)
            return None

    @execute_with_async_client(client_type=AWSClientType.S3)
    async def get_file_metadata(
        self, uri: str, client: AioBaseClient = None, log_exception: bool = True
    ) -> dict | None:
        """
        Retrieves only the metadata of a file without downloading its content.

        Args:
            uri: The S3 URI of the file
            client: Optional S3 client to use
            log_exception: Whether to log exceptions

        Returns:
            The metadata dictionary or None if the request fails
        """
        try:
            parsed_uri = S3Uri(uri)

            # Only get object metadata without the body
            response = await client.head_object(Bucket=parsed_uri.bucket, Key=parsed_uri.key)
            return response.get("Metadata", {})
        except Exception:
            if log_exception:
                LOG.exception("S3 metadata retrieval failed", uri=uri)
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
                    ExpiresIn=settings.PRESIGNED_URL_EXPIRATION,
                )
                presigned_urls.append(url)

            return presigned_urls
        except Exception:
            LOG.exception("Failed to create presigned url for S3 objects.", uris=uris)
            return None

    @execute_with_async_client(client_type=AWSClientType.S3)
    async def list_files(self, uri: str, client: AioBaseClient = None) -> list[str]:
        object_keys: list[str] = []
        parsed_uri = S3Uri(uri)
        async for page in client.get_paginator("list_objects_v2").paginate(
            Bucket=parsed_uri.bucket, Prefix=parsed_uri.key
        ):
            if "Contents" in page:
                for obj in page["Contents"]:
                    object_keys.append(obj["Key"])
        return object_keys

    @execute_with_async_client(client_type=AWSClientType.ECS)
    async def run_task(
        self,
        cluster: str,
        launch_type: str,
        task_definition: str,
        subnets: list[str],
        security_groups: list[str],
        client: AioBaseClient = None,
    ) -> dict:
        return await client.run_task(
            cluster=cluster,
            launchType=launch_type,
            taskDefinition=task_definition,
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": subnets,
                    "securityGroups": security_groups,
                    "assignPublicIp": "DISABLED",
                }
            },
        )

    @execute_with_async_client(client_type=AWSClientType.ECS)
    async def stop_task(self, cluster: str, task: str, client: AioBaseClient = None) -> dict:
        response = await client.stop_task(cluster=cluster, task=task)
        return response

    @execute_with_async_client(client_type=AWSClientType.ECS)
    async def describe_tasks(self, cluster: str, tasks: list[str], client: AioBaseClient = None) -> dict:
        response = await client.describe_tasks(cluster=cluster, tasks=tasks)
        return response

    @execute_with_async_client(client_type=AWSClientType.ECS)
    async def list_tasks(self, cluster: str, client: AioBaseClient = None) -> dict:
        response = await client.list_tasks(cluster=cluster)
        return response

    @execute_with_async_client(client_type=AWSClientType.ECS)
    async def describe_task_definition(self, task_definition: str, client: AioBaseClient = None) -> dict:
        return await client.describe_task_definition(taskDefinition=task_definition)

    @execute_with_async_client(client_type=AWSClientType.ECS)
    async def deregister_task_definition(self, task_definition: str, client: AioBaseClient = None) -> dict:
        return await client.deregister_task_definition(taskDefinition=task_definition)


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


aws_client = AsyncAWSClient()
