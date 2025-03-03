import asyncio
import time
from collections import defaultdict

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType, LogEntityType
from skyvern.forge.sdk.db.id import generate_artifact_id
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.ai_suggestions import AISuggestion
from skyvern.forge.sdk.schemas.task_v2 import TaskV2, Thought
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock

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
        thought_id: str | None = None,
        task_v2_id: str | None = None,
        ai_suggestion_id: str | None = None,
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
            thought_id=thought_id,
            task_v2_id=task_v2_id,
            organization_id=organization_id,
            ai_suggestion_id=ai_suggestion_id,
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

    async def create_log_artifact(
        self,
        log_entity_type: LogEntityType,
        log_entity_id: str,
        artifact_type: ArtifactType,
        step_id: str | None = None,
        task_id: str | None = None,
        workflow_run_id: str | None = None,
        workflow_run_block_id: str | None = None,
        organization_id: str | None = None,
        data: bytes | None = None,
        path: str | None = None,
    ) -> str:
        artifact_id = generate_artifact_id()
        uri = app.STORAGE.build_log_uri(log_entity_type, log_entity_id, artifact_type)
        return await self._create_artifact(
            aio_task_primary_key=log_entity_id,
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            uri=uri,
            step_id=step_id,
            task_id=task_id,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            organization_id=organization_id,
            data=data,
            path=path,
        )

    async def create_thought_artifact(
        self,
        thought: Thought,
        artifact_type: ArtifactType,
        data: bytes | None = None,
        path: str | None = None,
    ) -> str:
        artifact_id = generate_artifact_id()
        uri = app.STORAGE.build_thought_uri(artifact_id, thought, artifact_type)
        return await self._create_artifact(
            aio_task_primary_key=thought.observer_cruise_id,
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            uri=uri,
            thought_id=thought.observer_thought_id,
            task_v2_id=thought.observer_cruise_id,
            organization_id=thought.organization_id,
            data=data,
            path=path,
        )

    async def create_task_v2_artifact(
        self,
        task_v2: TaskV2,
        artifact_type: ArtifactType,
        data: bytes | None = None,
        path: str | None = None,
    ) -> str:
        artifact_id = generate_artifact_id()
        uri = app.STORAGE.build_task_v2_uri(artifact_id, task_v2, artifact_type)
        return await self._create_artifact(
            aio_task_primary_key=task_v2.observer_cruise_id,
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            uri=uri,
            task_v2_id=task_v2.observer_cruise_id,
            organization_id=task_v2.organization_id,
            data=data,
            path=path,
        )

    async def create_workflow_run_block_artifact(
        self,
        workflow_run_block: WorkflowRunBlock,
        artifact_type: ArtifactType,
        data: bytes | None = None,
        path: str | None = None,
    ) -> str:
        artifact_id = generate_artifact_id()
        uri = app.STORAGE.build_workflow_run_block_uri(artifact_id, workflow_run_block, artifact_type)
        return await self._create_artifact(
            aio_task_primary_key=workflow_run_block.workflow_run_block_id,
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            uri=uri,
            workflow_run_block_id=workflow_run_block.workflow_run_block_id,
            workflow_run_id=workflow_run_block.workflow_run_id,
            organization_id=workflow_run_block.organization_id,
            data=data,
            path=path,
        )

    async def create_ai_suggestion_artifact(
        self,
        ai_suggestion: AISuggestion,
        artifact_type: ArtifactType,
        data: bytes | None = None,
        path: str | None = None,
    ) -> str:
        artifact_id = generate_artifact_id()
        uri = app.STORAGE.build_ai_suggestion_uri(artifact_id, ai_suggestion, artifact_type)
        return await self._create_artifact(
            aio_task_primary_key=ai_suggestion.ai_suggestion_id,
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            uri=uri,
            ai_suggestion_id=ai_suggestion.ai_suggestion_id,
            organization_id=ai_suggestion.organization_id,
            data=data,
            path=path,
        )

    async def create_llm_artifact(
        self,
        data: bytes,
        artifact_type: ArtifactType,
        screenshots: list[bytes] | None = None,
        step: Step | None = None,
        thought: Thought | None = None,
        task_v2: TaskV2 | None = None,
        ai_suggestion: AISuggestion | None = None,
    ) -> None:
        if step:
            await self.create_artifact(
                step=step,
                artifact_type=artifact_type,
                data=data,
            )
            for screenshot in screenshots or []:
                await self.create_artifact(
                    step=step,
                    artifact_type=ArtifactType.SCREENSHOT_LLM,
                    data=screenshot,
                )
        elif task_v2:
            await self.create_task_v2_artifact(
                task_v2=task_v2,
                artifact_type=artifact_type,
                data=data,
            )
            for screenshot in screenshots or []:
                await self.create_task_v2_artifact(
                    task_v2=task_v2,
                    artifact_type=ArtifactType.SCREENSHOT_LLM,
                    data=screenshot,
                )
        elif thought:
            await self.create_thought_artifact(
                thought=thought,
                artifact_type=artifact_type,
                data=data,
            )
            for screenshot in screenshots or []:
                await self.create_thought_artifact(
                    thought=thought,
                    artifact_type=ArtifactType.SCREENSHOT_LLM,
                    data=screenshot,
                )
        elif ai_suggestion:
            await self.create_ai_suggestion_artifact(
                ai_suggestion=ai_suggestion,
                artifact_type=artifact_type,
                data=data,
            )
            for screenshot in screenshots or []:
                await self.create_ai_suggestion_artifact(
                    ai_suggestion=ai_suggestion,
                    artifact_type=ArtifactType.SCREENSHOT_LLM,
                    data=screenshot,
                )

    async def update_artifact_data(
        self,
        artifact_id: str | None,
        organization_id: str | None,
        data: bytes,
        primary_key: str = "task_id",
    ) -> None:
        if not artifact_id or not organization_id:
            return None
        artifact = await app.DATABASE.get_artifact_by_id(artifact_id, organization_id)
        if not artifact:
            return
        # Fire and forget
        aio_task = asyncio.create_task(app.STORAGE.store_artifact(artifact, data))

        if not artifact[primary_key]:
            raise ValueError(f"{primary_key} is required to update artifact data.")
        self.upload_aiotasks_map[artifact[primary_key]].append(aio_task)

    async def retrieve_artifact(self, artifact: Artifact) -> bytes | None:
        return await app.STORAGE.retrieve_artifact(artifact)

    async def get_share_link(self, artifact: Artifact) -> str | None:
        return await app.STORAGE.get_share_link(artifact)

    async def get_share_links(self, artifacts: list[Artifact]) -> list[str] | None:
        return await app.STORAGE.get_share_links(artifacts)

    async def wait_for_upload_aiotasks(self, primary_keys: list[str]) -> None:
        try:
            st = time.time()
            async with asyncio.timeout(30):
                await asyncio.gather(
                    *[
                        aio_task
                        for primary_key in primary_keys
                        for aio_task in self.upload_aiotasks_map[primary_key]
                        if not aio_task.done()
                    ]
                )
            LOG.info(
                f"S3 upload aio tasks for primary_keys={primary_keys} completed in {time.time() - st:.2f}s",
                primary_keys=primary_keys,
                duration=time.time() - st,
            )
        except asyncio.TimeoutError:
            LOG.error(
                f"Timeout (30s) while waiting for upload aio tasks for primary_keys={primary_keys}",
                primary_keys=primary_keys,
            )

        for primary_key in primary_keys:
            del self.upload_aiotasks_map[primary_key]
