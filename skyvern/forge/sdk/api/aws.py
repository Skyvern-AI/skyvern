from enum import StrEnum
from typing import IO, Any
from urllib.parse import urlparse

import aioboto3
import structlog

from skyvern.config import settings

LOG = structlog.get_logger()


class AWSClientType(StrEnum):
    S3 = "s3"
    SECRETS_MANAGER = "secretsmanager"
    ECS = "ecs"


class AsyncAWSClient:
    def __init__(
        self,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        region_name: str | None = None,
    ) -> None:
        self.aws_access_key_id = aws_access_key_id
        self.aws_secret_access_key = aws_secret_access_key
        self.region_name = region_name or settings.AWS_REGION
        self.session = aioboto3.Session(
            aws_access_key_id=self.aws_access_key_id,
            aws_secret_access_key=self.aws_secret_access_key,
        )

    async def get_secret(self, secret_name: str) -> str | None:
        try:
            async with self.session.client(AWSClientType.SECRETS_MANAGER, region_name=self.region_name) as client:
                response = await client.get_secret_value(SecretId=secret_name)
                return response["SecretString"]
        except Exception as e:
            try:
                error_code = e.response["Error"]["Code"]  # type: ignore
            except Exception:
                error_code = "failed-to-get-error-code"
            LOG.exception("Failed to get secret.", secret_name=secret_name, error_code=error_code)
            return None

    async def create_secret(self, secret_name: str, secret_value: str) -> None:
        try:
            async with self.session.client(AWSClientType.SECRETS_MANAGER, region_name=self.region_name) as client:
                await client.create_secret(Name=secret_name, SecretString=secret_value)
        except Exception as e:
            LOG.exception("Failed to create secret.", secret_name=secret_name)
            raise e

    async def set_secret(self, secret_name: str, secret_value: str) -> None:
        try:
            async with self.session.client(AWSClientType.SECRETS_MANAGER, region_name=self.region_name) as client:
                await client.put_secret_value(SecretId=secret_name, SecretString=secret_value)
        except Exception as e:
            LOG.exception("Failed to set secret.", secret_name=secret_name)
            raise e

    async def delete_secret(self, secret_name: str) -> None:
        try:
            async with self.session.client(AWSClientType.SECRETS_MANAGER, region_name=self.region_name) as client:
                await client.delete_secret(SecretId=secret_name)
        except Exception as e:
            LOG.exception("Failed to delete secret.", secret_name=secret_name)
            raise e

    async def upload_file(self, uri: str, data: bytes) -> str | None:
        try:
            async with self.session.client(AWSClientType.S3, region_name=self.region_name) as client:
                parsed_uri = S3Uri(uri)
                await client.put_object(Body=data, Bucket=parsed_uri.bucket, Key=parsed_uri.key)
                return uri
        except Exception:
            LOG.exception("S3 upload failed.", uri=uri)
            return None

    async def upload_file_stream(self, uri: str, file_obj: IO[bytes]) -> str | None:
        try:
            async with self.session.client(AWSClientType.S3, region_name=self.region_name) as client:
                parsed_uri = S3Uri(uri)
                await client.upload_fileobj(file_obj, parsed_uri.bucket, parsed_uri.key)
                LOG.debug("Upload file stream success", uri=uri)
                return uri
        except Exception:
            LOG.exception("S3 upload stream failed.", uri=uri)
            return None

    async def upload_file_from_path(
        self,
        uri: str,
        file_path: str,
        metadata: dict | None = None,
        raise_exception: bool = False,
    ) -> None:
        try:
            async with self.session.client(AWSClientType.S3, region_name=self.region_name) as client:
                parsed_uri = S3Uri(uri)
                params: dict[str, Any] = {
                    "Filename": file_path,
                    "Bucket": parsed_uri.bucket,
                    "Key": parsed_uri.key,
                }

                if metadata:
                    params["ExtraArgs"] = {"Metadata": metadata}

                await client.upload_file(**params)
        except Exception as e:
            LOG.exception("S3 upload failed.", uri=uri)
            if raise_exception:
                raise e

    async def download_file(self, uri: str, log_exception: bool = True) -> bytes | None:
        try:
            async with self.session.client(AWSClientType.S3, region_name=self.region_name) as client:
                parsed_uri = S3Uri(uri)

                # Get full object including body
                response = await client.get_object(Bucket=parsed_uri.bucket, Key=parsed_uri.key)
                return await response["Body"].read()
        except Exception:
            if log_exception:
                LOG.exception("S3 download failed", uri=uri)
            return None

    async def get_file_metadata(
        self,
        uri: str,
        log_exception: bool = True,
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
            async with self.session.client(AWSClientType.S3, region_name=self.region_name) as client:
                parsed_uri = S3Uri(uri)

                # Only get object metadata without the body
                response = await client.head_object(Bucket=parsed_uri.bucket, Key=parsed_uri.key)
                return response.get("Metadata", {})
        except Exception:
            if log_exception:
                LOG.exception("S3 metadata retrieval failed", uri=uri)
            return None

    async def create_presigned_urls(self, uris: list[str]) -> list[str] | None:
        presigned_urls = []
        try:
            async with self.session.client(AWSClientType.S3, region_name=self.region_name) as client:
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

    async def list_files(self, uri: str) -> list[str]:
        object_keys: list[str] = []
        parsed_uri = S3Uri(uri)
        async with self.session.client(AWSClientType.S3, region_name=self.region_name) as client:
            async for page in client.get_paginator("list_objects_v2").paginate(
                Bucket=parsed_uri.bucket, Prefix=parsed_uri.key
            ):
                if "Contents" in page:
                    for obj in page["Contents"]:
                        object_keys.append(obj["Key"])
            return object_keys

    async def run_task(
        self,
        cluster: str,
        launch_type: str,
        task_definition: str,
        subnets: list[str],
        security_groups: list[str],
    ) -> dict:
        async with self.session.client(AWSClientType.ECS, region_name=self.region_name) as client:
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

    async def stop_task(self, cluster: str, task: str, reason: str | None = None) -> dict:
        async with self.session.client(AWSClientType.ECS, region_name=self.region_name) as client:
            return await client.stop_task(cluster=cluster, task=task, reason=reason)

    async def describe_tasks(self, cluster: str, tasks: list[str]) -> dict:
        async with self.session.client(AWSClientType.ECS, region_name=self.region_name) as client:
            return await client.describe_tasks(cluster=cluster, tasks=tasks)

    async def list_tasks(self, cluster: str) -> dict:
        async with self.session.client(AWSClientType.ECS, region_name=self.region_name) as client:
            return await client.list_tasks(cluster=cluster)

    async def describe_task_definition(self, task_definition: str) -> dict:
        async with self.session.client(AWSClientType.ECS, region_name=self.region_name) as client:
            return await client.describe_task_definition(taskDefinition=task_definition)

    async def deregister_task_definition(self, task_definition: str) -> dict:
        async with self.session.client(AWSClientType.ECS, region_name=self.region_name) as client:
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
