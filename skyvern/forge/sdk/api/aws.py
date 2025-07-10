from enum import StrEnum
from typing import IO, Any
from urllib.parse import urlparse

import aioboto3
import structlog
from types_boto3_ec2.client import EC2Client
from types_boto3_ecs.client import ECSClient
from types_boto3_s3.client import S3Client
from types_boto3_secretsmanager.client import SecretsManagerClient

from skyvern.config import settings

LOG = structlog.get_logger()


# We only include the storage classes that we want to use in our application.
class S3StorageClass(StrEnum):
    STANDARD = "STANDARD"
    # REDUCED_REDUNDANCY = "REDUCED_REDUNDANCY"
    # INTELLIGENT_TIERING = "INTELLIGENT_TIERING"
    ONEZONE_IA = "ONEZONE_IA"
    GLACIER = "GLACIER"
    # DEEP_ARCHIVE = "DEEP_ARCHIVE"
    # OUTPOSTS = "OUTPOSTS"
    # STANDARD_IA = "STANDARD_IA"


class AWSClientType(StrEnum):
    S3 = "s3"
    SECRETS_MANAGER = "secretsmanager"
    ECS = "ecs"
    EC2 = "ec2"


class AsyncAWSClient:
    def __init__(
        self,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        region_name: str | None = None,
        endpoint_url: str | None = None,
    ) -> None:
        self.region_name = region_name or settings.AWS_REGION
        self._endpoint_url = endpoint_url
        self.session = aioboto3.Session(
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
        )

    def _ecs_client(self) -> ECSClient:
        return self.session.client(AWSClientType.ECS, region_name=self.region_name, endpoint_url=self._endpoint_url)

    def _secrets_manager_client(self) -> SecretsManagerClient:
        return self.session.client(
            AWSClientType.SECRETS_MANAGER, region_name=self.region_name, endpoint_url=self._endpoint_url
        )

    def _s3_client(self) -> S3Client:
        return self.session.client(AWSClientType.S3, region_name=self.region_name, endpoint_url=self._endpoint_url)

    def _ec2_client(self) -> EC2Client:
        return self.session.client(AWSClientType.EC2, region_name=self.region_name, endpoint_url=self._endpoint_url)

    def _create_tag_string(self, tags: dict[str, str]) -> str:
        return "&".join([f"{k}={v}" for k, v in tags.items()])

    async def get_secret(self, secret_name: str) -> str | None:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/secretsmanager/client/get_secret_value.html
        try:
            async with self._secrets_manager_client() as client:
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
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/secretsmanager/client/create_secret.html
        try:
            async with self._secrets_manager_client() as client:
                await client.create_secret(Name=secret_name, SecretString=secret_value)
        except Exception as e:
            LOG.exception("Failed to create secret.", secret_name=secret_name)
            raise e

    async def set_secret(self, secret_name: str, secret_value: str) -> None:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/secretsmanager/client/put_secret_value.html
        try:
            async with self._secrets_manager_client() as client:
                await client.put_secret_value(SecretId=secret_name, SecretString=secret_value)
        except Exception as e:
            LOG.exception("Failed to set secret.", secret_name=secret_name)
            raise e

    async def delete_secret(self, secret_name: str) -> None:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/secretsmanager/client/delete_secret.html
        try:
            async with self._secrets_manager_client() as client:
                await client.delete_secret(SecretId=secret_name)
        except Exception as e:
            LOG.exception("Failed to delete secret.", secret_name=secret_name)
            raise e

    async def upload_file(
        self,
        uri: str,
        data: bytes,
        storage_class: S3StorageClass = S3StorageClass.STANDARD,
        tags: dict[str, str] | None = None,
    ) -> str | None:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/client/put_object.html
        if storage_class not in S3StorageClass:
            raise ValueError(f"Invalid storage class: {storage_class}. Must be one of {list(S3StorageClass)}")
        try:
            async with self._s3_client() as client:
                parsed_uri = S3Uri(uri)
                extra_args = {"Tagging": self._create_tag_string(tags)} if tags else {}
                await client.put_object(
                    Body=data,
                    Bucket=parsed_uri.bucket,
                    Key=parsed_uri.key,
                    StorageClass=str(storage_class),
                    **extra_args,
                )
                return uri
        except Exception:
            LOG.exception("S3 upload failed.", uri=uri)
            return None

    async def upload_file_stream(
        self,
        uri: str,
        file_obj: IO[bytes],
        storage_class: S3StorageClass = S3StorageClass.STANDARD,
        tags: dict[str, str] | None = None,
    ) -> str | None:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/client/upload_fileobj.html#upload-fileobj
        if storage_class not in S3StorageClass:
            raise ValueError(f"Invalid storage class: {storage_class}. Must be one of {list(S3StorageClass)}")
        try:
            async with self._s3_client() as client:
                parsed_uri = S3Uri(uri)
                extra_args: dict[str, Any] = {"StorageClass": str(storage_class)}
                if tags:
                    extra_args["Tagging"] = self._create_tag_string(tags)
                await client.upload_fileobj(
                    file_obj,
                    parsed_uri.bucket,
                    parsed_uri.key,
                    ExtraArgs=extra_args,
                )
                LOG.debug("Upload file stream success", uri=uri)
                return uri
        except Exception:
            LOG.exception("S3 upload stream failed.", uri=uri)
            return None

    async def upload_file_from_path(
        self,
        uri: str,
        file_path: str,
        storage_class: S3StorageClass = S3StorageClass.STANDARD,
        metadata: dict | None = None,
        raise_exception: bool = False,
        tags: dict[str, str] | None = None,
    ) -> None:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/client/upload_file.html
        try:
            async with self._s3_client() as client:
                parsed_uri = S3Uri(uri)
                extra_args: dict[str, Any] = {"StorageClass": str(storage_class)}
                if metadata:
                    extra_args["Metadata"] = metadata
                if tags:
                    extra_args["Tagging"] = self._create_tag_string(tags)
                await client.upload_file(
                    Filename=file_path,
                    Bucket=parsed_uri.bucket,
                    Key=parsed_uri.key,
                    ExtraArgs=extra_args,
                )
        except Exception as e:
            LOG.exception("S3 upload failed.", uri=uri)
            if raise_exception:
                raise e

    async def download_file(self, uri: str, log_exception: bool = True) -> bytes | None:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/client/get_object.html
        try:
            async with self._s3_client() as client:
                parsed_uri = S3Uri(uri)

                # Get full object including body
                response = await client.get_object(Bucket=parsed_uri.bucket, Key=parsed_uri.key)
                return await response["Body"].read()
        except Exception:
            if log_exception:
                LOG.exception("S3 download failed", uri=uri)
            return None

    async def get_object_info(self, uri: str) -> dict:
        async with self._s3_client() as client:
            parsed_uri = S3Uri(uri)
            # Only get object metadata without the body
            return await client.head_object(Bucket=parsed_uri.bucket, Key=parsed_uri.key)

    async def get_file_metadata(
        self,
        uri: str,
        log_exception: bool = True,
    ) -> dict | None:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/client/head_object.html
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
            response = await self.get_object_info(uri)
            return response.get("Metadata", {})
        except Exception:
            if log_exception:
                LOG.exception("S3 metadata retrieval failed", uri=uri)
            return None

    async def create_presigned_urls(self, uris: list[str]) -> list[str] | None:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/client/generate_presigned_url.html
        presigned_urls = []
        try:
            async with self._s3_client() as client:
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
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/paginator/ListObjectsV2.html
        object_keys: list[str] = []
        parsed_uri = S3Uri(uri)
        async with self._s3_client() as client:
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
        assign_public_ip: str = "DISABLED",
        enable_execute_command: bool = False,
    ) -> dict:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ecs/client/run_task.html
        async with self._ecs_client() as client:
            return await client.run_task(
                cluster=cluster,
                launchType=launch_type,
                taskDefinition=task_definition,
                networkConfiguration={
                    "awsvpcConfiguration": {
                        "subnets": subnets,
                        "securityGroups": security_groups,
                        "assignPublicIp": assign_public_ip,
                    }
                },
                enableExecuteCommand=enable_execute_command,
            )

    async def stop_task(self, cluster: str, task: str, reason: str | None = None) -> dict:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ecs/client/stop_task.html
        async with self._ecs_client() as client:
            return await client.stop_task(cluster=cluster, task=task, reason=reason)

    async def describe_tasks(self, cluster: str, tasks: list[str]) -> dict:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ecs/client/describe_tasks.html
        async with self._ecs_client() as client:
            return await client.describe_tasks(cluster=cluster, tasks=tasks)

    async def list_tasks(self, cluster: str) -> dict:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ecs/client/list_tasks.html
        async with self._ecs_client() as client:
            return await client.list_tasks(cluster=cluster)

    async def describe_task_definition(self, task_definition: str) -> dict:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ecs/client/describe_task_definition.html
        async with self._ecs_client() as client:
            return await client.describe_task_definition(taskDefinition=task_definition)

    async def deregister_task_definition(self, task_definition: str) -> dict:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ecs/client/deregister_task_definition.html
        async with self._ecs_client() as client:
            return await client.deregister_task_definition(taskDefinition=task_definition)

    ###### EC2 ######
    async def describe_network_interfaces(self, network_interface_ids: list[str]) -> dict:
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ec2/client/describe_network_interfaces.html
        async with self._ec2_client() as client:
            return await client.describe_network_interfaces(NetworkInterfaceIds=network_interface_ids)


class S3Uri:
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

    def __str__(self) -> str:
        return self.uri


def tag_set_to_dict(tag_set: list[dict[str, str]]) -> dict[str, str]:
    """Convert a list of tags to a dictionary."""
    return {tag["Key"]: tag["Value"] for tag in tag_set}


aws_client = AsyncAWSClient()
