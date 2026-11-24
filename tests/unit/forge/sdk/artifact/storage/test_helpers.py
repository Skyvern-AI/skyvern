from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

from botocore.exceptions import ClientError
from pydantic import SecretStr

from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.ai_suggestions import AISuggestion
from skyvern.forge.sdk.schemas.task_v2 import TaskV2, Thought
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
from skyvern.schemas.workflows import BlockType

if TYPE_CHECKING:
    import pytest

    from skyvern.forge.sdk.artifact.storage.s3 import S3Storage


# Constants
TEST_ORGANIZATION_ID = "test-org-123"
TEST_TASK_ID = "tsk_123456789"


def artifact_root(octet: int = 65) -> SecretStr:
    return SecretStr(base64.b64encode(bytes([octet]) * 32).decode())


def create_fake_for_ai_suggestion(ai_suggestion_id: str) -> AISuggestion:
    return AISuggestion(
        ai_suggestion_id=ai_suggestion_id,
        organization_id=TEST_ORGANIZATION_ID,
        ai_suggestion_type="test_suggestion_type",
        created_at=datetime.utcnow(),
        modified_at=datetime.utcnow(),
    )


def create_fake_thought(cruise_id: str, thought_id: str) -> Thought:
    return Thought(
        observer_cruise_id=cruise_id,
        observer_thought_id=thought_id,
        created_at=datetime.utcnow(),
        modified_at=datetime.utcnow(),
        organization_id=TEST_ORGANIZATION_ID,
    )


def create_fake_step(step_id: str) -> Step:
    return Step(
        task_id=TEST_TASK_ID,
        order=1,
        retry_index=0,
        step_id=step_id,
        created_at=datetime.utcnow(),
        modified_at=datetime.utcnow(),
        status=StepStatus.created,
        is_last=False,
        organization_id=TEST_ORGANIZATION_ID,
    )


def create_fake_task_v2(observer_cruise_id: str) -> TaskV2:
    return TaskV2(
        observer_cruise_id=observer_cruise_id,
        created_at=datetime.utcnow(),
        modified_at=datetime.utcnow(),
        status=StepStatus.created,
        organization_id=TEST_ORGANIZATION_ID,
    )


def create_fake_workflow_run_block(workflow_run_id: str, workflow_run_block_id: str) -> WorkflowRunBlock:
    return WorkflowRunBlock(
        workflow_run_id=workflow_run_id,
        workflow_run_block_id=workflow_run_block_id,
        created_at=datetime.utcnow(),
        modified_at=datetime.utcnow(),
        organization_id=TEST_ORGANIZATION_ID,
        block_type=BlockType.TASK,
    )


class ProfileTransport:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.paths: list[Path] = []
        self.upload_policies: list[dict[str, object]] = []
        self.error: OSError | None = None
        self.fail_delete_uri: str | None = None
        self.pause_upload = False
        self.upload_started = asyncio.Event()
        self.upload_released = asyncio.Event()

    async def upload_file_from_path(self, uri: str, file_path: str, **policy: object) -> None:
        path = Path(file_path)
        self.paths.append(path)
        self.upload_policies.append(policy)
        self.upload_started.set()
        if self.pause_upload:
            await self.upload_released.wait()
        if self.error is not None:
            raise self.error
        self.objects[uri] = path.read_bytes()

    async def download_file(
        self, uri: str, *, on_version: Callable[[str], None] | None = None, **_: object
    ) -> bytes | None:
        body = self.objects.get(uri)
        if body is not None and on_version is not None:
            on_version(str(ProfileTransport._version(body)))
        return body

    @staticmethod
    def _version(body: bytes) -> int:
        return int.from_bytes(hashlib.sha256(body).digest()[:8])

    async def delete_file(self, uri: str) -> None:
        if uri == self.fail_delete_uri:
            self.fail_delete_uri = None
            raise OSError("legacy delete failed")
        self.objects.pop(uri, None)

    async def get_object_info(self, uri: str) -> dict | None:
        body = self.objects.get(uri)
        if body is None:
            return None
        digest = self._version(body)
        return {"ETag": str(digest), "Generation": digest}


def install_s3_profile_transport(storage: S3Storage, fake: ProfileTransport, monkeypatch: pytest.MonkeyPatch) -> None:
    class _S3Vendor:
        @staticmethod
        async def upload_file(*, Filename: str, Bucket: str, Key: str, ExtraArgs: dict) -> None:
            await fake.upload_file_from_path(f"s3://{Bucket}/{Key}", Filename, **ExtraArgs)

        @staticmethod
        async def get_object(*, Bucket: str, Key: str) -> dict:
            data = await fake.download_file(f"s3://{Bucket}/{Key}")
            if data is None:
                raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

            async def read() -> bytes:
                return data

            return {"Body": SimpleNamespace(read=read), "ETag": str(ProfileTransport._version(data))}

        @staticmethod
        async def head_object(*, Bucket: str, Key: str) -> dict:
            result = await fake.get_object_info(f"s3://{Bucket}/{Key}")
            if result is None:
                raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
            return result

        @staticmethod
        async def delete_object(*, Bucket: str, Key: str) -> None:
            await fake.delete_file(f"s3://{Bucket}/{Key}")

    @asynccontextmanager
    async def client():
        yield _S3Vendor()

    monkeypatch.setattr(storage.async_client, "_s3_client", client)
