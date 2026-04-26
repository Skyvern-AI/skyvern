import asyncio
import io
import time
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlencode

import structlog

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType, LogEntityType
from skyvern.forge.sdk.artifact.signing import parse_keyring, sign_artifact_url
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.db.id import generate_artifact_id
from skyvern.forge.sdk.db.models import ArtifactModel
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.ai_suggestions import AISuggestion
from skyvern.forge.sdk.schemas.task_v2 import TaskV2, Thought
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock

LOG = structlog.get_logger(__name__)

_SCREENSHOT_PREFIX_MAP: dict[ArtifactType, str] = {
    ArtifactType.SCREENSHOT_LLM: "screenshot_llm",
    ArtifactType.SCREENSHOT_ACTION: "screenshot_action",
    ArtifactType.SCREENSHOT_FINAL: "screenshot_final",
}


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


@dataclass
class StepArchiveAccumulator:
    """Accumulates all artifacts for a single step into one ZIP archive.

    All artifacts produced during a step (scrape data, LLM call data, action HTML,
    screenshots) are buffered here and flushed as a single S3 PUT at step completion.
    entries maps filename-within-ZIP → raw bytes.
    member_types records (artifact_type, filename, artifact_id) for each entry;
    artifact_id is pre-generated so callers can immediately link DB foreign keys
    (e.g. action.screenshot_artifact_id) before the archive is flushed.
    """

    step: Step
    workflow_run_id: str | None
    workflow_run_block_id: str | None
    run_id: str | None
    entries: dict[str, bytes] = field(default_factory=dict)
    # (artifact_type, filename, pre-generated artifact_id)
    member_types: list[tuple[ArtifactType, str, str]] = field(default_factory=list)
    # incremented each time accumulate_llm_call_to_archive is called so that
    # multiple LLM calls within one step produce distinct filenames instead of
    # silently overwriting each other.
    llm_call_count: int = 0
    # Deferred action.screenshot_artifact_id DB writes. Populated by
    # queue_action_screenshot_update() and applied in _flush_step_archive()
    # *after* bulk_create_artifacts() so the artifact row always exists first.
    pending_action_screenshot_updates: list[tuple[str, str, str]] = field(default_factory=list)
    # (organization_id, action_id, artifact_id)


class ArtifactManager:
    def __init__(self) -> None:
        # task_id -> list of aio_tasks for uploading artifacts
        self.upload_aiotasks_map: dict[str, list[asyncio.Task[None]]] = defaultdict(list)
        # step_id -> accumulator for step archive artifacts
        self._step_archives: dict[str, StepArchiveAccumulator] = {}

    @staticmethod
    def _build_artifact_model(
        artifact_id: str,
        artifact_type: ArtifactType,
        uri: str,
        organization_id: str,
        bundle_key: str | None = None,
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
            bundle_key=bundle_key,
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

        artifact = await app.DATABASE.artifacts.create_artifact(
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

    async def create_download_artifact(
        self,
        *,
        organization_id: str,
        run_id: str,
        uri: str,
        filename: str,
        workflow_run_id: str | None = None,
        checksum: str | None = None,
    ) -> str:
        """Register a downloaded file as an Artifact row without re-uploading.

        The bytes already live at ``uri`` (the uploads bucket). We only record a
        row so the file can be served through the signed ``/v1/artifacts/{id}/content``
        endpoint.
        """
        # Idempotent on (run_id, uri): if a DOWNLOAD artifact already exists for the
        # same physical file (e.g. a loop iteration re-uploads the same download dir),
        # return the existing artifact_id so signed URLs stay stable across calls —
        # otherwise ``loop_download_filter.to_downloaded_file_signature`` would treat
        # every iteration's URL as new.
        existing = await app.DATABASE.artifacts.find_download_artifact(
            organization_id=organization_id,
            run_id=run_id,
            uri=uri,
        )
        if existing is not None:
            return existing.artifact_id

        artifact_id = generate_artifact_id()
        context = skyvern_context.current()
        if workflow_run_id is None and context is not None:
            workflow_run_id = context.workflow_run_id
        await app.DATABASE.artifacts.create_artifact(
            artifact_id=artifact_id,
            artifact_type=ArtifactType.DOWNLOAD,
            uri=uri,
            organization_id=organization_id,
            run_id=run_id,
            workflow_run_id=workflow_run_id,
            checksum=checksum,
        )
        LOG.debug(
            "Registered downloaded file as artifact",
            artifact_id=artifact_id,
            run_id=run_id,
            filename=filename,
        )
        return artifact_id

    async def create_browser_session_download_artifact(
        self,
        *,
        organization_id: str,
        browser_session_id: str,
        uri: str,
        filename: str,
        checksum: str | None = None,
    ) -> str:
        """Register a session-scoped downloaded file as an Artifact row.

        Used by the browser_controller's watcher write site
        (``S3Storage.sync_browser_session_file(artifact_type="downloads")``).
        Idempotent on ``(organization_id, browser_session_id, uri)`` — the
        watcher fires repeatedly as a downloaded file grows, so we look up
        the existing row before inserting.

        ``run_id`` is intentionally NOT set here. The watcher runs in a
        separate process from the agent and does not know which run is
        currently using the session. Run finalization runs the
        ``claim_session_download_artifacts_for_run`` UPDATE to tag rows
        whose ``created_at`` falls inside the run's window.
        """
        existing = await app.DATABASE.artifacts.find_artifact_for_browser_session(
            organization_id=organization_id,
            browser_session_id=browser_session_id,
            uri=uri,
            artifact_type=ArtifactType.DOWNLOAD,
        )
        if existing is not None:
            return existing.artifact_id

        artifact_id = generate_artifact_id()
        await app.DATABASE.artifacts.create_artifact(
            artifact_id=artifact_id,
            artifact_type=ArtifactType.DOWNLOAD,
            uri=uri,
            organization_id=organization_id,
            browser_session_id=browser_session_id,
            checksum=checksum,
        )
        LOG.debug(
            "Registered session-scoped downloaded file as artifact",
            artifact_id=artifact_id,
            browser_session_id=browser_session_id,
            filename=filename,
        )
        return artifact_id

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

    async def _create_workflow_run_block_artifact_internal(
        self,
        workflow_run_block: WorkflowRunBlock,
        artifact_type: ArtifactType,
        data: bytes | None = None,
        path: str | None = None,
    ) -> tuple[str, str]:
        artifact_id = generate_artifact_id()
        uri = app.STORAGE.build_workflow_run_block_uri(
            organization_id=workflow_run_block.organization_id,
            artifact_id=artifact_id,
            workflow_run_block=workflow_run_block,
            artifact_type=artifact_type,
        )
        await self._create_artifact(
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
        return artifact_id, uri

    async def create_workflow_run_block_artifact(
        self,
        workflow_run_block: WorkflowRunBlock,
        artifact_type: ArtifactType,
        data: bytes | None = None,
        path: str | None = None,
    ) -> str:
        artifact_id, _ = await self._create_workflow_run_block_artifact_internal(
            workflow_run_block=workflow_run_block,
            artifact_type=artifact_type,
            data=data,
            path=path,
        )
        return artifact_id

    async def create_workflow_run_block_artifact_with_uri(
        self,
        workflow_run_block: WorkflowRunBlock,
        artifact_type: ArtifactType,
        data: bytes | None = None,
        path: str | None = None,
    ) -> tuple[str, str]:
        return await self._create_workflow_run_block_artifact_internal(
            workflow_run_block=workflow_run_block,
            artifact_type=artifact_type,
            data=data,
            path=path,
        )

    async def create_workflow_run_block_artifacts(
        self,
        workflow_run_block: WorkflowRunBlock,
        artifacts: list[tuple[ArtifactType, bytes]],
    ) -> list[str]:
        """
        Bulk-create artifacts for a workflow run block in a single DB round-trip.
        """
        if not artifacts:
            return []

        artifact_batch: list[ArtifactBatchData] = []
        for artifact_type, data in artifacts:
            artifact_id = generate_artifact_id()
            uri = app.STORAGE.build_workflow_run_block_uri(
                organization_id=workflow_run_block.organization_id,
                artifact_id=artifact_id,
                workflow_run_block=workflow_run_block,
                artifact_type=artifact_type,
            )
            artifact_batch.append(
                ArtifactBatchData(
                    artifact_model=self._build_artifact_model(
                        artifact_id=artifact_id,
                        artifact_type=artifact_type,
                        uri=uri,
                        organization_id=workflow_run_block.organization_id,
                        workflow_run_block_id=workflow_run_block.workflow_run_block_id,
                        workflow_run_id=workflow_run_block.workflow_run_id,
                    ),
                    data=data,
                )
            )

        request = BulkArtifactCreationRequest(
            artifacts=artifact_batch, primary_key=workflow_run_block.workflow_run_block_id
        )
        return await self._bulk_create_artifacts(request)

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
        artifacts = await app.DATABASE.artifacts.bulk_create_artifacts(artifact_models)

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
        artifact = await app.DATABASE.artifacts.get_artifact_by_id(artifact_id, organization_id)
        if not artifact:
            return
        # Fire and forget
        aio_task = asyncio.create_task(app.STORAGE.store_artifact(artifact, data))

        if not artifact[primary_key]:
            raise ValueError(f"{primary_key} is required to update artifact data.")
        self.upload_aiotasks_map[artifact[primary_key]].append(aio_task)

    async def retrieve_artifact(self, artifact: Artifact) -> bytes | None:
        return await app.STORAGE.retrieve_artifact(artifact)

    def build_signed_content_url(
        self,
        artifact_id: str,
        artifact_name: str | None = None,
        artifact_type: str | None = None,
    ) -> str:
        """Return a signed ``/v1/artifacts/{id}/content`` URL for any artifact.

        Non-bundled artifacts normally get a presigned S3 URL from
        ``STORAGE.get_share_link``. This method always builds the Skyvern-origin
        signed URL regardless of ``bundle_key`` — used for DOWNLOAD artifacts
        so webhook payloads stay short and clients hit our origin.
        """
        return self._bundle_content_url(
            artifact_id=artifact_id,
            artifact_name=artifact_name,
            artifact_type=artifact_type,
        )

    def _bundle_content_url(
        self,
        artifact_id: str,
        artifact_name: str | None = None,
        artifact_type: str | None = None,
    ) -> str:
        """Return an absolute URL for a bundled artifact served via the content endpoint.

        When ARTIFACT_CONTENT_HMAC_KEYRING is configured the URL is HMAC-SHA256 signed
        and carries expiry/kid/sig query parameters so the endpoint can authenticate
        requests without an org-level API key.

        artifact_name and artifact_type are appended as informational query params
        for client use only — they are not part of the signature.
        """
        base = settings.SKYVERN_BASE_URL.rstrip("/")
        if settings.ARTIFACT_CONTENT_HMAC_KEYRING:
            keyring = parse_keyring(settings.ARTIFACT_CONTENT_HMAC_KEYRING)
            return sign_artifact_url(
                base_url=base,
                artifact_id=artifact_id,
                keyring=keyring,
                artifact_name=artifact_name,
                artifact_type=artifact_type,
            )
        path = f"{base}/v1/artifacts/{artifact_id}/content"
        extra: dict[str, str] = {}
        if artifact_name is not None:
            extra["artifact_name"] = artifact_name
        if artifact_type is not None:
            extra["artifact_type"] = artifact_type
        return f"{path}?{urlencode(extra)}" if extra else path

    async def get_share_link(self, artifact: Artifact) -> str | None:
        if artifact.bundle_key:
            return self._bundle_content_url(
                artifact.artifact_id,
                artifact_name=artifact.bundle_key,
                artifact_type=artifact.artifact_type,
            )
        return await app.STORAGE.get_share_link(artifact)

    async def get_share_links(self, artifacts: list[Artifact]) -> list[str | None]:
        """Return share links for a list of artifacts, with bundle support.

        Bundled artifacts (those with a bundle_key) return a backend content-endpoint URL
        rather than a presigned S3 URL, which would 403 because the underlying S3 object is
        a ZIP that cannot be accessed directly.
        """
        return await self.get_share_links_with_bundle_support(artifacts)

    async def get_share_links_with_bundle_support(self, artifacts: list[Artifact]) -> list[str | None]:
        """Get share links; bundled artifacts return an absolute backend content-endpoint URL instead of a presigned URL."""
        result: list[str | None] = [None] * len(artifacts)
        non_bundle_indices: list[int] = []
        non_bundle_artifacts: list[Artifact] = []

        for i, artifact in enumerate(artifacts):
            if artifact.bundle_key:
                result[i] = self._bundle_content_url(
                    artifact.artifact_id,
                    artifact_name=artifact.bundle_key,
                    artifact_type=artifact.artifact_type,
                )
            else:
                non_bundle_indices.append(i)
                non_bundle_artifacts.append(artifact)

        LOG.debug(
            "get_share_links_with_bundle_support",
            total=len(artifacts),
            bundled=len(artifacts) - len(non_bundle_artifacts),
            non_bundled=len(non_bundle_artifacts),
        )

        if non_bundle_artifacts:
            signed_urls = await app.STORAGE.get_share_links(non_bundle_artifacts)
            if signed_urls:
                for idx, url in zip(non_bundle_indices, signed_urls):
                    result[idx] = url

        return result

    # ---------------------------------------------------------------------------
    # Step-archive accumulation helpers
    # ---------------------------------------------------------------------------

    def _get_or_create_step_archive(
        self,
        step: Step,
        workflow_run_id: str | None,
        workflow_run_block_id: str | None,
        run_id: str | None,
    ) -> StepArchiveAccumulator:
        if step.step_id not in self._step_archives:
            context = skyvern_context.current()
            self._step_archives[step.step_id] = StepArchiveAccumulator(
                step=step,
                workflow_run_id=workflow_run_id or (context.workflow_run_id if context else None),
                workflow_run_block_id=workflow_run_block_id
                or (context.workflow_run_block_id if context else None)
                or (context.parent_workflow_run_block_id if context else None),
                run_id=run_id or (context.run_id if context else None),
            )
        return self._step_archives[step.step_id]

    def _add_to_step_archive(
        self,
        acc: StepArchiveAccumulator,
        filename: str,
        data: bytes,
        artifact_type: ArtifactType,
        artifact_id: str | None = None,
    ) -> str:
        """Add a single file to the accumulator, deduplicating by filename.

        Returns the artifact_id (pre-generated or provided) so callers can link it
        in DB foreign keys (e.g. action.screenshot_artifact_id) before flush.
        """
        acc.entries[filename] = data
        # Deduplicate by filename — update in place if it already exists
        for i, (_, fn, existing_id) in enumerate(acc.member_types):
            if fn == filename:
                acc.member_types[i] = (artifact_type, filename, existing_id)
                return existing_id
        aid = artifact_id or generate_artifact_id()
        acc.member_types.append((artifact_type, filename, aid))
        return aid

    def accumulate_scrape_to_archive(
        self,
        step: Step,
        html: bytes,
        id_css_map: bytes,
        id_frame_map: bytes,
        element_tree: bytes,
        element_tree_trimmed: bytes,
        element_tree_in_prompt: bytes,
        workflow_run_id: str | None = None,
        workflow_run_block_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        """Accumulate scrape artifacts into the step archive (replaces 6 individual S3 PUTs)."""
        acc = self._get_or_create_step_archive(step, workflow_run_id, workflow_run_block_id, run_id)
        self._add_to_step_archive(acc, "scrape.html", html, ArtifactType.HTML_SCRAPE)
        self._add_to_step_archive(acc, "id_css_map.json", id_css_map, ArtifactType.VISIBLE_ELEMENTS_ID_CSS_MAP)
        self._add_to_step_archive(acc, "id_frame_map.json", id_frame_map, ArtifactType.VISIBLE_ELEMENTS_ID_FRAME_MAP)
        self._add_to_step_archive(acc, "element_tree.json", element_tree, ArtifactType.VISIBLE_ELEMENTS_TREE)
        self._add_to_step_archive(
            acc, "element_tree_trimmed.json", element_tree_trimmed, ArtifactType.VISIBLE_ELEMENTS_TREE_TRIMMED
        )
        self._add_to_step_archive(
            acc, "element_tree_in_prompt.txt", element_tree_in_prompt, ArtifactType.VISIBLE_ELEMENTS_TREE_IN_PROMPT
        )

    def accumulate_llm_call_to_archive(
        self,
        step: Step,
        workflow_run_id: str | None = None,
        workflow_run_block_id: str | None = None,
        run_id: str | None = None,
        hashed_href_map: bytes | None = None,
        prompt: bytes | None = None,
        request: bytes | None = None,
        response: bytes | None = None,
        parsed_response: bytes | None = None,
        rendered_response: bytes | None = None,
    ) -> None:
        """Accumulate LLM call artifacts into the step archive (replaces up to 6 individual S3 PUTs).

        Uses a per-step call counter (llm_call_count) so that multiple LLM calls within
        the same step (e.g. extract-actions + check-user-goal) produce distinct filenames
        (llm_prompt_0.txt, llm_prompt_1.txt, …) instead of silently overwriting each other.
        """
        acc = self._get_or_create_step_archive(step, workflow_run_id, workflow_run_block_id, run_id)
        idx = acc.llm_call_count
        if hashed_href_map is not None:
            self._add_to_step_archive(acc, f"hashed_href_map_{idx}.json", hashed_href_map, ArtifactType.HASHED_HREF_MAP)
        if prompt is not None:
            self._add_to_step_archive(acc, f"llm_prompt_{idx}.txt", prompt, ArtifactType.LLM_PROMPT)
        if request is not None:
            self._add_to_step_archive(acc, f"llm_request_{idx}.json", request, ArtifactType.LLM_REQUEST)
        if response is not None:
            self._add_to_step_archive(acc, f"llm_response_{idx}.json", response, ArtifactType.LLM_RESPONSE)
        if parsed_response is not None:
            self._add_to_step_archive(
                acc, f"llm_response_parsed_{idx}.json", parsed_response, ArtifactType.LLM_RESPONSE_PARSED
            )
        if rendered_response is not None:
            self._add_to_step_archive(
                acc, f"llm_response_rendered_{idx}.json", rendered_response, ArtifactType.LLM_RESPONSE_RENDERED
            )
        acc.llm_call_count += 1

    def accumulate_action_html_to_archive(
        self,
        step: Step,
        html_action: bytes,
        workflow_run_id: str | None = None,
        workflow_run_block_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        """Accumulate an HTML_ACTION into the step archive (replaces 1 individual S3 PUT per action)."""
        acc = self._get_or_create_step_archive(step, workflow_run_id, workflow_run_block_id, run_id)
        action_idx = sum(1 for k in acc.entries if k.startswith("html_action_"))
        filename = f"html_action_{action_idx}.html"
        self._add_to_step_archive(acc, filename, html_action, ArtifactType.HTML_ACTION)

    def accumulate_screenshot_to_step_archive(
        self,
        step: Step,
        screenshots: list[bytes],
        artifact_type: ArtifactType,
        workflow_run_id: str | None = None,
        workflow_run_block_id: str | None = None,
        run_id: str | None = None,
    ) -> list[str]:
        """Accumulate screenshots into the step archive (replaces 1 individual S3 PUT per screenshot).

        Returns the pre-generated artifact_ids so callers can immediately link DB foreign keys
        (e.g. action.screenshot_artifact_id) before the archive is flushed.
        """
        acc = self._get_or_create_step_archive(step, workflow_run_id, workflow_run_block_id, run_id)
        prefix = _SCREENSHOT_PREFIX_MAP.get(artifact_type, "screenshot_action")
        artifact_ids: list[str] = []
        for screenshot_bytes in screenshots:
            idx = sum(1 for k in acc.entries if k.startswith(f"{prefix}_"))
            filename = f"{prefix}_{idx}.png"
            aid = self._add_to_step_archive(acc, filename, screenshot_bytes, artifact_type)
            artifact_ids.append(aid)
        return artifact_ids

    def queue_action_screenshot_update(
        self,
        step: Step,
        organization_id: str,
        action_id: str,
        artifact_id: str,
    ) -> None:
        """Defer action.screenshot_artifact_id DB write until _flush_step_archive.

        This ensures the artifact row (created by bulk_create_artifacts inside the flush)
        exists in the DB before the action row references it, preventing dangling foreign
        keys when a task fails between accumulation and flushing.
        """
        acc = self._step_archives.get(step.step_id)
        if acc is None:
            LOG.warning(
                "queue_action_screenshot_update called but no step archive found; skipping",
                step_id=step.step_id,
                action_id=action_id,
            )
            return
        acc.pending_action_screenshot_updates.append((organization_id, action_id, artifact_id))

    @staticmethod
    def _build_zip(entries: dict[str, bytes]) -> bytes:
        """Build an in-memory ZIP from a filename → bytes mapping.

        Text files (html, json, txt) are deflate-compressed; binary files (png, zip) are stored as-is.
        """
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for filename, data in entries.items():
                ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
                compress = zipfile.ZIP_STORED if ext in ("png", "zip", "webm") else zipfile.ZIP_DEFLATED
                zf.writestr(zipfile.ZipInfo(filename), data, compress_type=compress)
        return buf.getvalue()

    async def flush_step_archive(self, step_id: str) -> None:
        """Build the ZIP, upload as one S3 object, and create DB rows for all member artifacts.

        Call this as soon as a step finishes executing (before moving on to the next step) to
        release the in-memory buffer immediately rather than waiting until the end of the task.
        Safe to call multiple times for the same step_id — subsequent calls are no-ops because
        the accumulator is popped on the first flush.
        """
        accumulator = self._step_archives.pop(step_id, None)
        if not accumulator or not accumulator.entries or not accumulator.member_types:
            return

        step = accumulator.step
        LOG.debug(
            "Flushing step archive",
            step_id=step_id,
            artifact_types=[t.value for t, _, _ in accumulator.member_types],
            entry_count=len(accumulator.entries),
        )
        archive_artifact_id = generate_artifact_id()
        archive_uri = app.STORAGE.build_uri(
            organization_id=step.organization_id,
            artifact_id=archive_artifact_id,
            step=step,
            artifact_type=ArtifactType.STEP_ARCHIVE,
        )

        now = datetime.now(UTC)
        archive_artifact = Artifact(
            artifact_id=archive_artifact_id,
            artifact_type=ArtifactType.STEP_ARCHIVE,
            uri=archive_uri,
            organization_id=step.organization_id,
            step_id=step.step_id,
            task_id=step.task_id,
            workflow_run_id=accumulator.workflow_run_id,
            workflow_run_block_id=accumulator.workflow_run_block_id,
            run_id=accumulator.run_id,
            created_at=now,
            modified_at=now,
        )

        zip_bytes = self._build_zip(accumulator.entries)
        await app.STORAGE.store_artifact(archive_artifact, zip_bytes)

        # Parent archive row (no bundle_key — represents the ZIP object itself)
        parent_model = self._build_artifact_model(
            artifact_id=archive_artifact_id,
            artifact_type=ArtifactType.STEP_ARCHIVE,
            uri=archive_uri,
            organization_id=step.organization_id,
            step_id=step.step_id,
            task_id=step.task_id,
            workflow_run_id=accumulator.workflow_run_id,
            workflow_run_block_id=accumulator.workflow_run_block_id,
            run_id=accumulator.run_id,
        )
        # Member rows (bundle_key points to the filename inside the ZIP)
        member_models = [
            self._build_artifact_model(
                artifact_id=artifact_id,
                artifact_type=artifact_type,
                uri=archive_uri,
                bundle_key=filename,
                organization_id=step.organization_id,
                step_id=step.step_id,
                task_id=step.task_id,
                workflow_run_id=accumulator.workflow_run_id,
                workflow_run_block_id=accumulator.workflow_run_block_id,
                run_id=accumulator.run_id,
            )
            for artifact_type, filename, artifact_id in accumulator.member_types
        ]
        await app.DATABASE.artifacts.bulk_create_artifacts([parent_model, *member_models])

        # Apply deferred action.screenshot_artifact_id updates now that artifact rows exist.
        for organization_id, action_id, artifact_id in accumulator.pending_action_screenshot_updates:
            try:
                await app.DATABASE.artifacts.update_action_screenshot_artifact_id(
                    organization_id=organization_id,
                    action_id=action_id,
                    screenshot_artifact_id=artifact_id,
                )
            except Exception:
                LOG.warning(
                    "Failed to update action with screenshot artifact id after archive flush",
                    action_id=action_id,
                    artifact_id=artifact_id,
                    exc_info=True,
                )

    async def create_task_archive(
        self,
        step: Step,
        entries: dict[str, tuple[ArtifactType, bytes]],
        workflow_run_id: str | None = None,
        workflow_run_block_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        """Build and upload a task-level cleanup archive (HAR, console log, trace, final screenshot).

        entries maps filename → (ArtifactType, raw_bytes).
        """
        if not entries:
            return

        context = skyvern_context.current()
        archive_artifact_id = generate_artifact_id()
        archive_uri = app.STORAGE.build_uri(
            organization_id=step.organization_id,
            artifact_id=archive_artifact_id,
            step=step,
            artifact_type=ArtifactType.TASK_ARCHIVE,
        )

        now = datetime.now(UTC)
        archive_artifact = Artifact(
            artifact_id=archive_artifact_id,
            artifact_type=ArtifactType.TASK_ARCHIVE,
            uri=archive_uri,
            organization_id=step.organization_id,
            step_id=step.step_id,
            task_id=step.task_id,
            workflow_run_id=workflow_run_id or (context.workflow_run_id if context else None),
            workflow_run_block_id=workflow_run_block_id or (context.parent_workflow_run_block_id if context else None),
            run_id=run_id or (context.run_id if context else None),
            created_at=now,
            modified_at=now,
        )

        zip_entries = {filename: data for filename, (_, data) in entries.items()}
        zip_bytes = self._build_zip(zip_entries)
        await app.STORAGE.store_artifact(archive_artifact, zip_bytes)

        # Parent archive row (no bundle_key — represents the ZIP object itself)
        parent_model = self._build_artifact_model(
            artifact_id=archive_artifact_id,
            artifact_type=ArtifactType.TASK_ARCHIVE,
            uri=archive_uri,
            organization_id=step.organization_id,
            step_id=step.step_id,
            task_id=step.task_id,
            workflow_run_id=archive_artifact.workflow_run_id,
            workflow_run_block_id=archive_artifact.workflow_run_block_id,
            run_id=archive_artifact.run_id,
        )
        # Member rows (bundle_key points to the filename inside the ZIP)
        member_models = [
            self._build_artifact_model(
                artifact_id=generate_artifact_id(),
                artifact_type=artifact_type,
                uri=archive_uri,
                bundle_key=filename,
                organization_id=step.organization_id,
                step_id=step.step_id,
                task_id=step.task_id,
                workflow_run_id=archive_artifact.workflow_run_id,
                workflow_run_block_id=archive_artifact.workflow_run_block_id,
                run_id=archive_artifact.run_id,
            )
            for filename, (artifact_type, _) in entries.items()
        ]
        await app.DATABASE.artifacts.bulk_create_artifacts([parent_model, *member_models])

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

        # Flush any accumulated step archives for the given task IDs
        primary_key_set = set(primary_keys)
        step_ids_to_flush = [
            step_id for step_id, acc in list(self._step_archives.items()) if acc.step.task_id in primary_key_set
        ]
        for step_id in step_ids_to_flush:
            try:
                await self.flush_step_archive(step_id)
            except Exception:
                LOG.error("Failed to flush step archive", step_id=step_id, exc_info=True)

    def discard_step_archives(self, task_id: str) -> None:
        """Discard (without uploading) any buffered step archives for a task.

        Call this from exception/cancellation handlers to prevent the class-level
        dict from growing unbounded when a task fails before wait_for_upload_aiotasks.
        Logs a warning for each discarded archive so dropped data is visible in logs.
        """
        step_ids = [sid for sid, acc in list(self._step_archives.items()) if acc.step.task_id == task_id]
        for step_id in step_ids:
            acc = self._step_archives.pop(step_id, None)
            if acc:
                LOG.warning(
                    "Discarding unflushed step archive due to task failure or cancellation",
                    step_id=step_id,
                    task_id=task_id,
                    entry_count=len(acc.entries),
                    artifact_count=len(acc.member_types),
                )
