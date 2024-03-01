import asyncio
import time
from collections import defaultdict

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.db.id import generate_artifact_id
from skyvern.forge.sdk.models import Step

LOG = structlog.get_logger(__name__)


class ArtifactManager:
    # task_id -> list of aio_tasks for uploading artifacts
    upload_aiotasks_map: dict[str, list[asyncio.Task[None]]] = defaultdict(list)

    async def create_artifact(
        self, step: Step, artifact_type: ArtifactType, data: bytes | None = None, path: str | None = None
    ) -> str:
        # TODO (kerem): Which is better?
        #    current: (disadvantage: we create the artifact_id UUID here)
        #       1. generate artifact_id UUID here
        #       2. build uri with artifact_id, step_id, task_id, artifact_type
        #       3. create artifact in db using artifact_id, step_id, task_id, artifact_type, uri
        #       4. store artifact in storage
        #    alternative: (disadvantage: two db calls)
        #       1. create artifact in db without the URI
        #       2. build uri with artifact_id, step_id, task_id, artifact_type
        #       3. update artifact in db with the URI
        #       4. store artifact in storage
        if data is None and path is None:
            raise ValueError("Either data or path must be provided to create an artifact.")
        if data and path:
            raise ValueError("Both data and path cannot be provided to create an artifact.")
        artifact_id = generate_artifact_id()
        uri = app.STORAGE.build_uri(artifact_id, step, artifact_type)
        artifact = await app.DATABASE.create_artifact(
            artifact_id,
            step.step_id,
            step.task_id,
            artifact_type,
            uri,
            organization_id=step.organization_id,
        )
        if data:
            # Fire and forget
            aio_task = asyncio.create_task(app.STORAGE.store_artifact(artifact, data))
            self.upload_aiotasks_map[step.task_id].append(aio_task)
        elif path:
            # Fire and forget
            aio_task = asyncio.create_task(app.STORAGE.store_artifact_from_path(artifact, path))
            self.upload_aiotasks_map[step.task_id].append(aio_task)

        return artifact_id

    async def update_artifact_data(self, artifact_id: str | None, organization_id: str | None, data: bytes) -> None:
        if not artifact_id or not organization_id:
            return None
        artifact = await app.DATABASE.get_artifact_by_id(artifact_id, organization_id)
        if not artifact:
            return
        # Fire and forget
        aio_task = asyncio.create_task(app.STORAGE.store_artifact(artifact, data))
        self.upload_aiotasks_map[artifact.task_id].append(aio_task)

    async def retrieve_artifact(self, artifact: Artifact) -> bytes | None:
        return await app.STORAGE.retrieve_artifact(artifact)

    async def get_share_link(self, artifact: Artifact) -> str | None:
        return await app.STORAGE.get_share_link(artifact)

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
            LOG.error(f"Timeout (30s) while waiting for upload tasks for task_id={task_id}", task_id=task_id)

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
            LOG.error(f"Timeout (30s) while waiting for upload tasks for task_ids={task_ids}", task_ids=task_ids)

        for task_id in task_ids:
            del self.upload_aiotasks_map[task_id]
