import asyncio
import time
from collections import defaultdict
from typing import Literal

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.db.id import generate_artifact_id
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.observers import ObserverThought

LOG = structlog.get_logger(__name__)


class ArtifactManager:
    # task_id -> list of aio_tasks for uploading artifacts
    upload_aiotasks_map: dict[str, list[asyncio.Task[None]]] = defaultdict(list)

    async def _create_artifact(
        self,
        aio_task_primary_key: str,
        artifact_id: str,
        artifact_type: ArtifactType,
        uri: str,
        step_id: str | None = None,
        task_id: str | None = None,
        workflow_run_id: str | None = None,
        workflow_run_block_id: str | None = None,
        observer_thought_id: str | None = None,
        observer_cruise_id: str | None = None,
        organization_id: str | None = None,
        data: bytes | None = None,
        path: str | None = None,
    ) -> str:
        if data is None and path is None:
            raise ValueError("Either data or path must be provided to create an artifact.")
        if data and path:
            raise ValueError("Both data and path cannot be provided to create an artifact.")
        artifact = await app.DATABASE.create_artifact(
            artifact_id,
            artifact_type,
            uri,
            step_id=step_id,
            task_id=task_id,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            observer_thought_id=observer_thought_id,
            observer_cruise_id=observer_cruise_id,
            organization_id=organization_id,
        )
        if data:
            # Fire and forget
            aio_task = asyncio.create_task(app.STORAGE.store_artifact(artifact, data))
            self.upload_aiotasks_map[aio_task_primary_key].append(aio_task)
        elif path:
            # Fire and forget
            aio_task = asyncio.create_task(app.STORAGE.store_artifact_from_path(artifact, path))
            self.upload_aiotasks_map[aio_task_primary_key].append(aio_task)

        return artifact_id

    async def create_artifact(
        self,
        step: Step,
        artifact_type: ArtifactType,
        data: bytes | None = None,
        path: str | None = None,
    ) -> str:
        artifact_id = generate_artifact_id()
        uri = app.STORAGE.build_uri(artifact_id, step, artifact_type)
        return await self._create_artifact(
            aio_task_primary_key=step.task_id,
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            uri=uri,
            step_id=step.step_id,
            task_id=step.task_id,
            organization_id=step.organization_id,
            data=data,
            path=path,
        )

    async def create_observer_thought_artifact(
        self,
        observer_thought: ObserverThought,
        artifact_type: ArtifactType,
        data: bytes | None = None,
        path: str | None = None,
    ) -> str:
        artifact_id = generate_artifact_id()
        uri = app.STORAGE.build_observer_thought_uri(artifact_id, observer_thought, artifact_type)
        return await self._create_artifact(
            aio_task_primary_key=observer_thought.observer_cruise_id,
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            uri=uri,
            observer_thought_id=observer_thought.observer_thought_id,
            observer_cruise_id=observer_thought.observer_cruise_id,
            organization_id=observer_thought.organization_id,
            data=data,
            path=path,
        )

    async def update_artifact_data(
        self,
        artifact_id: str | None,
        organization_id: str | None,
        data: bytes,
        primary_key: Literal["task_id", "observer_thought_id"] = "task_id",
    ) -> None:
        if not artifact_id or not organization_id:
            return None
        artifact = await app.DATABASE.get_artifact_by_id(artifact_id, organization_id)
        if not artifact:
            return
        # Fire and forget
        aio_task = asyncio.create_task(app.STORAGE.store_artifact(artifact, data))
        if primary_key == "task_id":
            if not artifact.task_id:
                raise ValueError("Task ID is required to update artifact data.")
            self.upload_aiotasks_map[artifact.task_id].append(aio_task)
        elif primary_key == "observer_thought_id":
            if not artifact.observer_thought_id:
                raise ValueError("Observer Thought ID is required to update artifact data.")
            self.upload_aiotasks_map[artifact.observer_thought_id].append(aio_task)

    async def retrieve_artifact(self, artifact: Artifact) -> bytes | None:
        return await app.STORAGE.retrieve_artifact(artifact)

    async def get_share_link(self, artifact: Artifact) -> str | None:
        return await app.STORAGE.get_share_link(artifact)

    async def get_share_links(self, artifacts: list[Artifact]) -> list[str] | None:
        return await app.STORAGE.get_share_links(artifacts)

    async def wait_for_upload_aiotasks_for_task(self, task_id: str) -> None:
        try:
            st = time.time()
            async with asyncio.timeout(30):
                await asyncio.gather(
                    *[aio_task for aio_task in self.upload_aiotasks_map[task_id] if not aio_task.done()]
                )
            LOG.info(
                f"S3 upload tasks for task_id={task_id} completed in {time.time() - st:.2f}s",
                task_id=task_id,
                duration=time.time() - st,
            )
        except asyncio.TimeoutError:
            LOG.error(
                f"Timeout (30s) while waiting for upload tasks for task_id={task_id}",
                task_id=task_id,
            )

        del self.upload_aiotasks_map[task_id]

    async def wait_for_upload_aiotasks_for_tasks(self, task_ids: list[str]) -> None:
        try:
            st = time.time()
            async with asyncio.timeout(30):
                await asyncio.gather(
                    *[
                        aio_task
                        for task_id in task_ids
                        for aio_task in self.upload_aiotasks_map[task_id]
                        if not aio_task.done()
                    ]
                )
            LOG.info(
                f"S3 upload tasks for task_ids={task_ids} completed in {time.time() - st:.2f}s",
                task_ids=task_ids,
                duration=time.time() - st,
            )
        except asyncio.TimeoutError:
            LOG.error(
                f"Timeout (30s) while waiting for upload tasks for task_ids={task_ids}",
                task_ids=task_ids,
            )

        for task_id in task_ids:
            del self.upload_aiotasks_map[task_id]
