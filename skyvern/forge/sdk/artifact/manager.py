import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType, LogEntityType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.db.id import generate_artifact_id
from skyvern.forge.sdk.db.models import ArtifactModel
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.ai_suggestions import AISuggestion
from skyvern.forge.sdk.schemas.task_v2 import TaskV2, Thought
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock

LOG = structlog.get_logger(__name__)


@dataclass
class ArtifactBatchData:
    """
    Data class for batch artifact creation.

    Attributes:
        artifact_model: The ArtifactModel instance to insert
        data: Optional bytes data to upload
        path: Optional file path to upload from
    """

    artifact_model: ArtifactModel
    data: bytes | None = None
    path: str | None = None

    def __post_init__(self) -> None:
        """Validate that exactly one of data or path is provided."""
        if self.data is not None and self.path is not None:
            raise ValueError("Cannot specify both data and path for artifact upload")


@dataclass
class BulkArtifactCreationRequest:
    """
    Request data for bulk artifact creation.

    Attributes:
        artifacts: List of artifact batch data to create
        primary_key: Primary key for tracking upload tasks (e.g., task_id, cruise_id)
    """

    artifacts: list[ArtifactBatchData]
    primary_key: str


class ArtifactManager:
    # task_id -> list of aio_tasks for uploading artifacts
    upload_aiotasks_map: dict[str, list[asyncio.Task[None]]] = defaultdict(list)

    @staticmethod
    def _build_artifact_model(
        artifact_id: str,
        artifact_type: ArtifactType,
        uri: str,
        organization_id: str,
        step_id: str | None = None,
        task_id: str | None = None,
        workflow_run_id: str | None = None,
        workflow_run_block_id: str | None = None,
        thought_id: str | None = None,
        task_v2_id: str | None = None,
        run_id: str | None = None,
        ai_suggestion_id: str | None = None,
    ) -> ArtifactModel:
        """
        Helper function to build an ArtifactModel instance.

        Args:
            artifact_id: Unique artifact identifier
            artifact_type: Type of the artifact
            uri: Storage URI for the artifact
            organization_id: Organization ID
            step_id: Optional step ID
            task_id: Optional task ID
            workflow_run_id: Optional workflow run ID
            workflow_run_block_id: Optional workflow run block ID
            thought_id: Optional thought ID (stored as observer_thought_id)
            task_v2_id: Optional task v2 ID (stored as observer_cruise_id)
            run_id: Optional run ID
            ai_suggestion_id: Optional AI suggestion ID

        Returns:
            ArtifactModel instance ready for database insertion
        """
        return ArtifactModel(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            uri=uri,
            organization_id=organization_id,
            task_id=task_id,
            step_id=step_id,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            observer_cruise_id=task_v2_id,
            observer_thought_id=thought_id,
            run_id=run_id,
            ai_suggestion_id=ai_suggestion_id,
        )

    async def _create_artifact(
        self,
        aio_task_primary_key: str,
        artifact_id: str,
        artifact_type: ArtifactType,
        uri: str,
        organization_id: str,
        step_id: str | None = None,
        task_id: str | None = None,
        workflow_run_id: str | None = None,
        workflow_run_block_id: str | None = None,
        thought_id: str | None = None,
        task_v2_id: str | None = None,
        run_id: str | None = None,
        ai_suggestion_id: str | None = None,
        data: bytes | None = None,
        path: str | None = None,
    ) -> str:
        if data is None and path is None:
            raise ValueError("Either data or path must be provided to create an artifact.")
        if data and path:
            raise ValueError("Both data and path cannot be provided to create an artifact.")

        context = skyvern_context.current()
        if not workflow_run_id and context:
            workflow_run_id = context.workflow_run_id
        if not task_v2_id and context:
            task_v2_id = context.task_v2_id
        if not task_id and context:
            task_id = context.task_id
        if not run_id and context:
            run_id = context.run_id
        if not workflow_run_block_id and context:
            workflow_run_block_id = context.parent_workflow_run_block_id

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
            run_id=run_id,
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
        uri = app.STORAGE.build_uri(
            organization_id=step.organization_id, artifact_id=artifact_id, step=step, artifact_type=artifact_type
        )
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
        *,
        log_entity_type: LogEntityType,
        log_entity_id: str,
        artifact_type: ArtifactType,
        organization_id: str,
        step_id: str | None = None,
        task_id: str | None = None,
        workflow_run_id: str | None = None,
        workflow_run_block_id: str | None = None,
        data: bytes | None = None,
        path: str | None = None,
    ) -> str:
        artifact_id = generate_artifact_id()
        uri = app.STORAGE.build_log_uri(
            organization_id=organization_id,
            log_entity_type=log_entity_type,
            log_entity_id=log_entity_id,
            artifact_type=artifact_type,
        )
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
        uri = app.STORAGE.build_thought_uri(
            organization_id=thought.organization_id,
            artifact_id=artifact_id,
            thought=thought,
            artifact_type=artifact_type,
        )
        return await self._create_artifact(
            aio_task_primary_key=thought.observer_cruise_id,
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            uri=uri,
            thought_id=thought.observer_thought_id,
            task_v2_id=thought.observer_cruise_id,
            workflow_run_id=thought.workflow_run_id,
            workflow_run_block_id=thought.workflow_run_block_id,
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
        uri = app.STORAGE.build_task_v2_uri(
            organization_id=task_v2.organization_id,
            artifact_id=artifact_id,
            task_v2=task_v2,
            artifact_type=artifact_type,
        )
        return await self._create_artifact(
            aio_task_primary_key=task_v2.observer_cruise_id,
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            uri=uri,
            task_v2_id=task_v2.observer_cruise_id,
            workflow_run_id=task_v2.workflow_run_id,
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
        uri = app.STORAGE.build_workflow_run_block_uri(
            organization_id=workflow_run_block.organization_id,
            artifact_id=artifact_id,
            workflow_run_block=workflow_run_block,
            artifact_type=artifact_type,
        )
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
        uri = app.STORAGE.build_ai_suggestion_uri(
            organization_id=ai_suggestion.organization_id,
            artifact_id=artifact_id,
            ai_suggestion=ai_suggestion,
            artifact_type=artifact_type,
        )
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

    async def create_script_file_artifact(
        self,
        *,
        organization_id: str,
        script_id: str,
        script_version: int,
        file_path: str,
        data: bytes,
    ) -> str:
        """Create an artifact for a script file.

        Args:
            organization_id: The organization ID
            script_id: The script ID
            script_version: The script version
            file_path: The file path relative to script root
            data: The file content as bytes

        Returns:
            The artifact ID
        """
        artifact_id = generate_artifact_id()
        uri = app.STORAGE.build_script_file_uri(
            organization_id=organization_id,
            script_id=script_id,
            script_version=script_version,
            file_path=file_path,
        )
        return await self._create_artifact(
            aio_task_primary_key=f"{script_id}_{script_version}",
            artifact_id=artifact_id,
            artifact_type=ArtifactType.SCRIPT_FILE,
            uri=uri,
            organization_id=organization_id,
            data=data,
        )

    async def bulk_create_artifacts(
        self,
        requests: list[BulkArtifactCreationRequest | None],
    ) -> list[str]:
        artifacts: list[ArtifactBatchData] = []
        primary_key: str | None = None
        for request in requests:
            if request:
                artifacts.extend(request.artifacts)
                primary_key = request.primary_key

        if primary_key is None or not artifacts:
            return []

        return await self._bulk_create_artifacts(
            BulkArtifactCreationRequest(artifacts=artifacts, primary_key=primary_key)
        )

    async def _bulk_create_artifacts(
        self,
        request: BulkArtifactCreationRequest,
    ) -> list[str]:
        """
        Bulk create multiple artifacts in a single database transaction.

        Args:
            request: BulkArtifactCreationRequest containing artifacts and primary key

        Returns:
            List of artifact IDs
        """
        if not request.artifacts:
            return []

        # Extract models for bulk insert
        artifact_models = [artifact_data.artifact_model for artifact_data in request.artifacts]

        # Bulk insert artifacts
        artifacts = await app.DATABASE.bulk_create_artifacts(artifact_models)

        # Fire and forget upload tasks
        for artifact, artifact_data in zip(artifacts, request.artifacts):
            if artifact_data.data is not None:
                aio_task = asyncio.create_task(app.STORAGE.store_artifact(artifact, artifact_data.data))
                self.upload_aiotasks_map[request.primary_key].append(aio_task)
            elif artifact_data.path is not None:
                aio_task = asyncio.create_task(app.STORAGE.store_artifact_from_path(artifact, artifact_data.path))
                self.upload_aiotasks_map[request.primary_key].append(aio_task)

        return [model.artifact_id for model in artifact_models]

    def _prepare_step_artifacts(
        self,
        step: Step,
        artifact_type: ArtifactType,
        data: bytes,
        screenshots: list[bytes] | None = None,
    ) -> BulkArtifactCreationRequest:
        """Helper to prepare artifact batch request for Step-based artifacts."""
        artifacts = []

        # Main artifact
        artifact_id = generate_artifact_id()
        uri = app.STORAGE.build_uri(
            organization_id=step.organization_id,
            artifact_id=artifact_id,
            step=step,
            artifact_type=artifact_type,
        )
        artifacts.append(
            ArtifactBatchData(
                artifact_model=self._build_artifact_model(
                    artifact_id=artifact_id,
                    artifact_type=artifact_type,
                    uri=uri,
                    organization_id=step.organization_id,
                    step_id=step.step_id,
                    task_id=step.task_id,
                ),
                data=data,
            )
        )

        # Screenshot artifacts
        for screenshot in screenshots or []:
            screenshot_id = generate_artifact_id()
            screenshot_uri = app.STORAGE.build_uri(
                organization_id=step.organization_id,
                artifact_id=screenshot_id,
                step=step,
                artifact_type=ArtifactType.SCREENSHOT_LLM,
            )
            artifacts.append(
                ArtifactBatchData(
                    artifact_model=self._build_artifact_model(
                        artifact_id=screenshot_id,
                        artifact_type=ArtifactType.SCREENSHOT_LLM,
                        uri=screenshot_uri,
                        organization_id=step.organization_id,
                        step_id=step.step_id,
                        task_id=step.task_id,
                    ),
                    data=screenshot,
                )
            )

        return BulkArtifactCreationRequest(artifacts=artifacts, primary_key=step.task_id)

    def _prepare_task_v2_artifacts(
        self,
        task_v2: TaskV2,
        artifact_type: ArtifactType,
        data: bytes,
        screenshots: list[bytes] | None = None,
    ) -> BulkArtifactCreationRequest:
        """Helper to prepare artifact batch request for TaskV2-based artifacts."""
        context = skyvern_context.current()
        workflow_run_id = context.workflow_run_id if context else task_v2.workflow_run_id
        workflow_run_block_id = context.parent_workflow_run_block_id if context else None

        artifacts = []

        # Main artifact
        artifact_id = generate_artifact_id()
        uri = app.STORAGE.build_task_v2_uri(
            organization_id=task_v2.organization_id,
            artifact_id=artifact_id,
            task_v2=task_v2,
            artifact_type=artifact_type,
        )
        artifacts.append(
            ArtifactBatchData(
                artifact_model=self._build_artifact_model(
                    artifact_id=artifact_id,
                    artifact_type=artifact_type,
                    uri=uri,
                    organization_id=task_v2.organization_id,
                    task_v2_id=task_v2.observer_cruise_id,
                    workflow_run_id=workflow_run_id,
                    workflow_run_block_id=workflow_run_block_id,
                ),
                data=data,
            )
        )

        # Screenshot artifacts
        for screenshot in screenshots or []:
            screenshot_id = generate_artifact_id()
            screenshot_uri = app.STORAGE.build_task_v2_uri(
                organization_id=task_v2.organization_id,
                artifact_id=screenshot_id,
                task_v2=task_v2,
                artifact_type=ArtifactType.SCREENSHOT_LLM,
            )
            artifacts.append(
                ArtifactBatchData(
                    artifact_model=self._build_artifact_model(
                        artifact_id=screenshot_id,
                        artifact_type=ArtifactType.SCREENSHOT_LLM,
                        uri=screenshot_uri,
                        organization_id=task_v2.organization_id,
                        task_v2_id=task_v2.observer_cruise_id,
                        workflow_run_id=workflow_run_id,
                        workflow_run_block_id=workflow_run_block_id,
                    ),
                    data=screenshot,
                )
            )

        return BulkArtifactCreationRequest(artifacts=artifacts, primary_key=task_v2.observer_cruise_id)

    def _prepare_thought_artifacts(
        self,
        thought: Thought,
        artifact_type: ArtifactType,
        data: bytes,
        screenshots: list[bytes] | None = None,
    ) -> BulkArtifactCreationRequest:
        """Helper to prepare artifact batch request for Thought-based artifacts."""
        artifacts = []

        # Main artifact
        artifact_id = generate_artifact_id()
        uri = app.STORAGE.build_thought_uri(
            organization_id=thought.organization_id,
            artifact_id=artifact_id,
            thought=thought,
            artifact_type=artifact_type,
        )
        artifacts.append(
            ArtifactBatchData(
                artifact_model=self._build_artifact_model(
                    artifact_id=artifact_id,
                    artifact_type=artifact_type,
                    uri=uri,
                    organization_id=thought.organization_id,
                    thought_id=thought.observer_thought_id,
                    task_v2_id=thought.observer_cruise_id,
                    workflow_run_id=thought.workflow_run_id,
                    workflow_run_block_id=thought.workflow_run_block_id,
                ),
                data=data,
            )
        )

        # Screenshot artifacts
        for screenshot in screenshots or []:
            screenshot_id = generate_artifact_id()
            screenshot_uri = app.STORAGE.build_thought_uri(
                organization_id=thought.organization_id,
                artifact_id=screenshot_id,
                thought=thought,
                artifact_type=ArtifactType.SCREENSHOT_LLM,
            )
            artifacts.append(
                ArtifactBatchData(
                    artifact_model=self._build_artifact_model(
                        artifact_id=screenshot_id,
                        artifact_type=ArtifactType.SCREENSHOT_LLM,
                        uri=screenshot_uri,
                        organization_id=thought.organization_id,
                        thought_id=thought.observer_thought_id,
                        task_v2_id=thought.observer_cruise_id,
                        workflow_run_id=thought.workflow_run_id,
                        workflow_run_block_id=thought.workflow_run_block_id,
                    ),
                    data=screenshot,
                )
            )

        return BulkArtifactCreationRequest(artifacts=artifacts, primary_key=thought.observer_cruise_id)

    def _prepare_ai_suggestion_artifacts(
        self,
        ai_suggestion: AISuggestion,
        artifact_type: ArtifactType,
        data: bytes,
        screenshots: list[bytes] | None = None,
    ) -> BulkArtifactCreationRequest:
        """Helper to prepare artifact batch request for AISuggestion-based artifacts."""
        artifacts = []

        # Main artifact
        artifact_id = generate_artifact_id()
        uri = app.STORAGE.build_ai_suggestion_uri(
            organization_id=ai_suggestion.organization_id,
            artifact_id=artifact_id,
            ai_suggestion=ai_suggestion,
            artifact_type=artifact_type,
        )
        artifacts.append(
            ArtifactBatchData(
                artifact_model=self._build_artifact_model(
                    artifact_id=artifact_id,
                    artifact_type=artifact_type,
                    uri=uri,
                    organization_id=ai_suggestion.organization_id,
                    ai_suggestion_id=ai_suggestion.ai_suggestion_id,
                ),
                data=data,
            )
        )

        # Screenshot artifacts
        for screenshot in screenshots or []:
            screenshot_id = generate_artifact_id()
            screenshot_uri = app.STORAGE.build_ai_suggestion_uri(
                organization_id=ai_suggestion.organization_id,
                artifact_id=screenshot_id,
                ai_suggestion=ai_suggestion,
                artifact_type=ArtifactType.SCREENSHOT_LLM,
            )
            artifacts.append(
                ArtifactBatchData(
                    artifact_model=self._build_artifact_model(
                        artifact_id=screenshot_id,
                        artifact_type=ArtifactType.SCREENSHOT_LLM,
                        uri=screenshot_uri,
                        organization_id=ai_suggestion.organization_id,
                        ai_suggestion_id=ai_suggestion.ai_suggestion_id,
                    ),
                    data=screenshot,
                )
            )

        return BulkArtifactCreationRequest(artifacts=artifacts, primary_key=ai_suggestion.ai_suggestion_id)

    async def prepare_llm_artifact(
        self,
        data: bytes,
        artifact_type: ArtifactType,
        screenshots: list[bytes] | None = None,
        step: Step | None = None,
        thought: Thought | None = None,
        task_v2: TaskV2 | None = None,
        ai_suggestion: AISuggestion | None = None,
    ) -> BulkArtifactCreationRequest | None:
        if step:
            return self._prepare_step_artifacts(
                step=step,
                artifact_type=artifact_type,
                data=data,
                screenshots=screenshots,
            )

        elif task_v2:
            return self._prepare_task_v2_artifacts(
                task_v2=task_v2,
                artifact_type=artifact_type,
                data=data,
                screenshots=screenshots,
            )

        elif thought:
            return self._prepare_thought_artifacts(
                thought=thought,
                artifact_type=artifact_type,
                data=data,
                screenshots=screenshots,
            )

        elif ai_suggestion:
            return self._prepare_ai_suggestion_artifacts(
                ai_suggestion=ai_suggestion,
                artifact_type=artifact_type,
                data=data,
                screenshots=screenshots,
            )
        else:
            return None

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
                f"Saving artifacts - aio tasks for primary_keys={primary_keys} completed in {time.time() - st:.2f}s",
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
