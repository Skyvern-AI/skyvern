import asyncio
from typing import Any

import structlog
from sqlalchemy import (
    event,
    pool,
)
from sqlalchemy.exc import (
    SQLAlchemyError,
)
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from skyvern.config import settings
from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB
from skyvern.forge.sdk.db.exceptions import ScheduleLimitExceededError  # noqa: F401
from skyvern.forge.sdk.db.models import PersistentBrowserSessionModel
from skyvern.forge.sdk.db.repositories.artifacts import ArtifactsRepository
from skyvern.forge.sdk.db.repositories.browser_sessions import BrowserSessionsRepository
from skyvern.forge.sdk.db.repositories.credentials import CredentialRepository
from skyvern.forge.sdk.db.repositories.debug import DebugRepository
from skyvern.forge.sdk.db.repositories.folders import FoldersRepository
from skyvern.forge.sdk.db.repositories.observer import ObserverRepository
from skyvern.forge.sdk.db.repositories.organizations import OrganizationsRepository
from skyvern.forge.sdk.db.repositories.otp import OTPRepository
from skyvern.forge.sdk.db.repositories.schedules import SchedulesRepository
from skyvern.forge.sdk.db.repositories.scripts import ScriptsRepository
from skyvern.forge.sdk.db.repositories.tasks import TasksRepository
from skyvern.forge.sdk.db.repositories.workflow_parameters import WorkflowParametersRepository
from skyvern.forge.sdk.db.repositories.workflow_runs import WorkflowRunsRepository
from skyvern.forge.sdk.db.repositories.workflows import WorkflowsRepository
from skyvern.forge.sdk.db.utils import (
    _custom_json_serializer,
)
from skyvern.forge.sdk.trace import traced

LOG = structlog.get_logger()


def _build_engine(database_string: str) -> AsyncEngine:
    """
    Build a SQLAlchemy async engine.

    Supports both PostgreSQL and SQLite (via aiosqlite) dialects.

    PostgreSQL behaviour:
      When DISABLE_CONNECTION_POOL=True (NullPool): enforce statement_timeout
      and allow prepared statements.
      When DISABLE_CONNECTION_POOL=False (QueuePool): disable prepared statements
      and do not set statement_timeout - set at role level in the database,
      since the transaction pooler does not maintain session-level settings.

    SQLite behaviour:
      For :memory: databases, uses StaticPool to keep the single connection alive.
      For file-backed databases, enables WAL mode for concurrent read support.
      Always enables foreign key enforcement via PRAGMA.
    """
    if database_string.startswith("sqlite"):
        from skyvern.config import _ensure_sqlite_dir

        _ensure_sqlite_dir(database_string)
        is_memory = ":memory:" in database_string
        engine_kwargs: dict[str, Any] = {
            "json_serializer": _custom_json_serializer,
        }
        if is_memory:
            engine_kwargs["poolclass"] = pool.StaticPool
            engine_kwargs["connect_args"] = {"check_same_thread": False}
        engine = create_async_engine(database_string, **engine_kwargs)

        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn: Any, connection_record: Any) -> None:
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            if not is_memory:
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        return engine

    # PostgreSQL path (unchanged)
    connect_args: dict[str, Any] = {}
    if settings.DISABLE_CONNECTION_POOL:
        if "postgresql+psycopg" in database_string:
            connect_args["options"] = f"-c statement_timeout={settings.DATABASE_STATEMENT_TIMEOUT_MS}"
        if "postgresql+asyncpg" in database_string:
            connect_args["server_settings"] = {"statement_timeout": str(settings.DATABASE_STATEMENT_TIMEOUT_MS)}
        return create_async_engine(
            database_string,
            json_serializer=_custom_json_serializer,
            connect_args=connect_args,
            poolclass=pool.NullPool,
        )

    else:
        if "postgresql+psycopg" in database_string:
            connect_args["prepare_threshold"] = None
        if "postgresql+asyncpg" in database_string:
            connect_args["statement_cache_size"] = 0
        return create_async_engine(
            database_string,
            json_serializer=_custom_json_serializer,
            connect_args=connect_args,
            pool_pre_ping=True,
            pool_size=settings.DATABASE_POOL_SIZE,
            max_overflow=settings.DATABASE_POOL_MAX_OVERFLOW,
        )


__all__ = ["AgentDB", "ScheduleLimitExceededError"]


class AgentDB(BaseAlchemyDB):
    def __init__(self, database_string: str, debug_enabled: bool = False, db_engine: AsyncEngine | None = None) -> None:
        super().__init__(db_engine or _build_engine(database_string))
        self.debug_enabled = debug_enabled
        # Global lock for SQLite schedule serialization. Unlike Postgres advisory locks
        # (which are scoped per org:workflow via hashtext(key)), this serializes ALL
        # schedule creates across all workflows. Acceptable for single-user embedded mode.
        self._sqlite_schedule_lock: asyncio.Lock | None = (
            asyncio.Lock() if self.engine.dialect.name == "sqlite" else None
        )

        # -- Zero-dependency repositories --
        self.tasks = TasksRepository(self.Session, debug_enabled, self.is_retryable_error)
        self.workflows = WorkflowsRepository(self.Session, debug_enabled, self.is_retryable_error)
        self.workflow_params = WorkflowParametersRepository(self.Session, debug_enabled, self.is_retryable_error)
        self.credentials = CredentialRepository(self.Session, debug_enabled, self.is_retryable_error)
        self.otp = OTPRepository(self.Session, debug_enabled, self.is_retryable_error)
        self.debug = DebugRepository(self.Session, debug_enabled, self.is_retryable_error)
        self.organizations = OrganizationsRepository(self.Session, debug_enabled, self.is_retryable_error)
        self.scripts = ScriptsRepository(self.Session, debug_enabled, self.is_retryable_error)
        self.browser_sessions = BrowserSessionsRepository(self.Session, debug_enabled, self.is_retryable_error)
        self.schedules = SchedulesRepository(
            self.Session,
            debug_enabled,
            self.is_retryable_error,
            sqlite_schedule_lock=self._sqlite_schedule_lock,
        )

        # -- Cross-dependency repositories --
        self.workflow_runs = WorkflowRunsRepository(
            self.Session,
            debug_enabled,
            self.is_retryable_error,
            workflow_parameter_reader=self.workflow_params,
            dialect_name=self.engine.dialect.name,
        )
        self.artifacts = ArtifactsRepository(
            self.Session,
            debug_enabled,
            self.is_retryable_error,
            run_reader=self.workflow_runs,
        )
        self.folders = FoldersRepository(
            self.Session,
            debug_enabled,
            self.is_retryable_error,
            workflow_reader=self.workflows,
        )
        self.observer = ObserverRepository(
            self.Session,
            debug_enabled,
            self.is_retryable_error,
            task_reader=self.tasks,
        )

    def is_retryable_error(self, error: SQLAlchemyError) -> bool:
        error_msg = str(error).lower()
        return "server closed the connection" in error_msg

    # ======================================================================
    # Backward-compatible delegate methods
    # ======================================================================

    # -- Task delegates --

    async def create_task(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.create_task(*args, **kwargs)

    async def create_step(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.create_step(*args, **kwargs)

    async def get_task(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.get_task(*args, **kwargs)

    async def get_tasks_by_ids(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.get_tasks_by_ids(*args, **kwargs)

    async def get_step(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.get_step(*args, **kwargs)

    async def get_task_steps(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.get_task_steps(*args, **kwargs)

    async def get_steps_by_task_ids(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.get_steps_by_task_ids(*args, **kwargs)

    async def get_total_unique_step_order_count_by_task_ids(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.get_total_unique_step_order_count_by_task_ids(*args, **kwargs)

    async def get_workflow_run_progress_timestamps(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.get_workflow_run_progress_timestamps(*args, **kwargs)

    async def get_task_step_models(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.get_task_step_models(*args, **kwargs)

    async def get_task_step_count(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.get_task_step_count(*args, **kwargs)

    async def get_task_actions(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.get_task_actions(*args, **kwargs)

    async def get_task_actions_hydrated(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.get_task_actions_hydrated(*args, **kwargs)

    async def get_tasks_actions(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.get_tasks_actions(*args, **kwargs)

    async def get_action_count_for_step(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.get_action_count_for_step(*args, **kwargs)

    async def get_first_step(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.get_first_step(*args, **kwargs)

    async def get_latest_step(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.get_latest_step(*args, **kwargs)

    @traced(name="skyvern.db.update_step")
    async def update_step(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.update_step(*args, **kwargs)

    async def clear_task_failure_reason(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.clear_task_failure_reason(*args, **kwargs)

    @traced(name="skyvern.db.update_task")
    async def update_task(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.update_task(*args, **kwargs)

    async def update_task_2fa_state(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.update_task_2fa_state(*args, **kwargs)

    async def bulk_update_tasks(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.bulk_update_tasks(*args, **kwargs)

    async def get_tasks(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.get_tasks(*args, **kwargs)

    async def get_tasks_count(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.get_tasks_count(*args, **kwargs)

    async def get_running_tasks_info_globally(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.get_running_tasks_info_globally(*args, **kwargs)

    async def get_latest_task_by_workflow_id(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.get_latest_task_by_workflow_id(*args, **kwargs)

    async def get_last_task_for_workflow_run(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.get_last_task_for_workflow_run(*args, **kwargs)

    async def get_tasks_by_workflow_run_id(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.get_tasks_by_workflow_run_id(*args, **kwargs)

    async def delete_task_steps(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.delete_task_steps(*args, **kwargs)

    async def get_previous_actions_for_task(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.get_previous_actions_for_task(*args, **kwargs)

    async def delete_task_actions(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.delete_task_actions(*args, **kwargs)

    # -- Workflow run delegates --

    async def get_running_workflow_runs_info_globally(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_runs.get_running_workflow_runs_info_globally(*args, **kwargs)

    async def create_workflow_run(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_runs.create_workflow_run(*args, **kwargs)

    async def update_workflow_run(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_runs.update_workflow_run(*args, **kwargs)

    async def bulk_update_workflow_runs(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_runs.bulk_update_workflow_runs(*args, **kwargs)

    async def clear_workflow_run_failure_reason(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_runs.clear_workflow_run_failure_reason(*args, **kwargs)

    async def get_all_runs(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_runs.get_all_runs(*args, **kwargs)

    async def get_all_runs_v2(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_runs.get_all_runs_v2(*args, **kwargs)

    async def get_workflow_run(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_runs.get_workflow_run(*args, **kwargs)

    async def get_last_queued_workflow_run(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_runs.get_last_queued_workflow_run(*args, **kwargs)

    async def get_workflow_runs_by_ids(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_runs.get_workflow_runs_by_ids(*args, **kwargs)

    async def get_last_running_workflow_run(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_runs.get_last_running_workflow_run(*args, **kwargs)

    async def get_last_workflow_run_for_browser_session(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_runs.get_last_workflow_run_for_browser_session(*args, **kwargs)

    async def get_last_workflow_run_for_browser_address(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_runs.get_last_workflow_run_for_browser_address(*args, **kwargs)

    async def get_workflows_depending_on(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_runs.get_workflows_depending_on(*args, **kwargs)

    async def get_workflow_runs(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_runs.get_workflow_runs(*args, **kwargs)

    async def get_workflow_runs_count(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_runs.get_workflow_runs_count(*args, **kwargs)

    async def get_workflow_runs_for_workflow_permanent_id(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_runs.get_workflow_runs_for_workflow_permanent_id(*args, **kwargs)

    async def get_workflow_runs_by_parent_workflow_run_id(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_runs.get_workflow_runs_by_parent_workflow_run_id(*args, **kwargs)

    async def get_workflow_run_output_parameters(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_runs.get_workflow_run_output_parameters(*args, **kwargs)

    async def get_workflow_run_output_parameter_by_id(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_runs.get_workflow_run_output_parameter_by_id(*args, **kwargs)

    async def create_or_update_workflow_run_output_parameter(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_runs.create_or_update_workflow_run_output_parameter(*args, **kwargs)

    async def update_workflow_run_output_parameter(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_runs.update_workflow_run_output_parameter(*args, **kwargs)

    async def create_workflow_run_parameter(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_runs.create_workflow_run_parameter(*args, **kwargs)

    async def create_workflow_run_parameters(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_runs.create_workflow_run_parameters(*args, **kwargs)

    async def get_workflow_run_parameters(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_runs.get_workflow_run_parameters(*args, **kwargs)

    async def get_workflow_run_block_errors(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_runs.get_workflow_run_block_errors(*args, **kwargs)

    # -- Workflow parameter delegates --

    async def create_workflow_parameter(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_params.create_workflow_parameter(*args, **kwargs)

    async def create_aws_secret_parameter(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_params.create_aws_secret_parameter(*args, **kwargs)

    async def create_output_parameter(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_params.create_output_parameter(*args, **kwargs)

    async def save_workflow_definition_parameters(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_params.save_workflow_definition_parameters(*args, **kwargs)

    async def get_workflow_output_parameters(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_params.get_workflow_output_parameters(*args, **kwargs)

    async def get_workflow_output_parameters_by_ids(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_params.get_workflow_output_parameters_by_ids(*args, **kwargs)

    async def get_workflow_parameters(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_params.get_workflow_parameters(*args, **kwargs)

    async def get_workflow_parameter(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_params.get_workflow_parameter(*args, **kwargs)

    async def create_task_generation(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_params.create_task_generation(*args, **kwargs)

    async def create_ai_suggestion(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_params.create_ai_suggestion(*args, **kwargs)

    async def create_workflow_copilot_chat(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_params.create_workflow_copilot_chat(*args, **kwargs)

    async def update_workflow_copilot_chat(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_params.update_workflow_copilot_chat(*args, **kwargs)

    async def create_workflow_copilot_chat_message(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_params.create_workflow_copilot_chat_message(*args, **kwargs)

    async def get_workflow_copilot_chat_messages(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_params.get_workflow_copilot_chat_messages(*args, **kwargs)

    async def get_workflow_copilot_chat_by_id(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_params.get_workflow_copilot_chat_by_id(*args, **kwargs)

    async def get_latest_workflow_copilot_chat(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_params.get_latest_workflow_copilot_chat(*args, **kwargs)

    async def get_task_generation_by_prompt_hash(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_params.get_task_generation_by_prompt_hash(*args, **kwargs)

    @traced(name="skyvern.db.create_action")
    async def create_action(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_params.create_action(*args, **kwargs)

    async def update_action_reasoning(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_params.update_action_reasoning(*args, **kwargs)

    async def retrieve_action_plan(self, *args: Any, **kwargs: Any) -> Any:
        return await self.workflow_params.retrieve_action_plan(*args, **kwargs)

    async def create_task_run(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.create_task_run(*args, **kwargs)

    async def update_task_run(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.update_task_run(*args, **kwargs)

    async def sync_task_run_status(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.sync_task_run_status(*args, **kwargs)

    async def update_job_run_compute_cost(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.update_job_run_compute_cost(*args, **kwargs)

    async def cache_task_run(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.cache_task_run(*args, **kwargs)

    async def get_cached_task_run(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.get_cached_task_run(*args, **kwargs)

    async def get_run(self, *args: Any, **kwargs: Any) -> Any:
        return await self.tasks.get_run(*args, **kwargs)

    # -- Artifact delegates --

    @traced(name="skyvern.db.create_artifact")
    async def create_artifact(self, *args: Any, **kwargs: Any) -> Any:
        return await self.artifacts.create_artifact(*args, **kwargs)

    @traced(name="skyvern.db.bulk_create_artifacts")
    async def bulk_create_artifacts(self, *args: Any, **kwargs: Any) -> Any:
        return await self.artifacts.bulk_create_artifacts(*args, **kwargs)

    async def get_artifacts_for_task_v2(self, *args: Any, **kwargs: Any) -> Any:
        return await self.artifacts.get_artifacts_for_task_v2(*args, **kwargs)

    async def get_artifacts_for_task_step(self, *args: Any, **kwargs: Any) -> Any:
        return await self.artifacts.get_artifacts_for_task_step(*args, **kwargs)

    async def get_artifacts_for_run(self, *args: Any, **kwargs: Any) -> Any:
        return await self.artifacts.get_artifacts_for_run(*args, **kwargs)

    async def get_artifact_by_id(self, *args: Any, **kwargs: Any) -> Any:
        return await self.artifacts.get_artifact_by_id(*args, **kwargs)

    async def get_artifacts_by_ids(self, *args: Any, **kwargs: Any) -> Any:
        return await self.artifacts.get_artifacts_by_ids(*args, **kwargs)

    async def get_artifacts_by_entity_id(self, *args: Any, **kwargs: Any) -> Any:
        return await self.artifacts.get_artifacts_by_entity_id(*args, **kwargs)

    async def get_artifact_by_entity_id(self, *args: Any, **kwargs: Any) -> Any:
        return await self.artifacts.get_artifact_by_entity_id(*args, **kwargs)

    async def get_artifact(self, *args: Any, **kwargs: Any) -> Any:
        return await self.artifacts.get_artifact(*args, **kwargs)

    async def get_artifact_for_run(self, *args: Any, **kwargs: Any) -> Any:
        return await self.artifacts.get_artifact_for_run(*args, **kwargs)

    async def get_latest_artifact(self, *args: Any, **kwargs: Any) -> Any:
        return await self.artifacts.get_latest_artifact(*args, **kwargs)

    async def get_latest_n_artifacts(self, *args: Any, **kwargs: Any) -> Any:
        return await self.artifacts.get_latest_n_artifacts(*args, **kwargs)

    async def delete_task_artifacts(self, *args: Any, **kwargs: Any) -> Any:
        return await self.artifacts.delete_task_artifacts(*args, **kwargs)

    async def delete_task_v2_artifacts(self, *args: Any, **kwargs: Any) -> Any:
        return await self.artifacts.delete_task_v2_artifacts(*args, **kwargs)

    async def update_action_screenshot_artifact_id(self, *args: Any, **kwargs: Any) -> Any:
        return await self.artifacts.update_action_screenshot_artifact_id(*args, **kwargs)

    # -- Browser session delegates --

    async def create_browser_profile(self, *args: Any, **kwargs: Any) -> Any:
        return await self.browser_sessions.create_browser_profile(*args, **kwargs)

    async def get_browser_profile(self, *args: Any, **kwargs: Any) -> Any:
        return await self.browser_sessions.get_browser_profile(*args, **kwargs)

    async def list_browser_profiles(self, *args: Any, **kwargs: Any) -> Any:
        return await self.browser_sessions.list_browser_profiles(*args, **kwargs)

    async def delete_browser_profile(self, *args: Any, **kwargs: Any) -> Any:
        return await self.browser_sessions.delete_browser_profile(*args, **kwargs)

    async def get_active_persistent_browser_sessions(self, *args: Any, **kwargs: Any) -> Any:
        return await self.browser_sessions.get_active_persistent_browser_sessions(*args, **kwargs)

    async def get_persistent_browser_sessions_history(self, *args: Any, **kwargs: Any) -> Any:
        return await self.browser_sessions.get_persistent_browser_sessions_history(*args, **kwargs)

    async def get_persistent_browser_session_by_runnable_id(self, *args: Any, **kwargs: Any) -> Any:
        return await self.browser_sessions.get_persistent_browser_session_by_runnable_id(*args, **kwargs)

    async def get_persistent_browser_session(self, *args: Any, **kwargs: Any) -> Any:
        return await self.browser_sessions.get_persistent_browser_session(*args, **kwargs)

    async def create_persistent_browser_session(self, *args: Any, **kwargs: Any) -> Any:
        return await self.browser_sessions.create_persistent_browser_session(*args, **kwargs)

    async def update_persistent_browser_session(self, *args: Any, **kwargs: Any) -> Any:
        return await self.browser_sessions.update_persistent_browser_session(*args, **kwargs)

    async def set_persistent_browser_session_browser_address(self, *args: Any, **kwargs: Any) -> Any:
        return await self.browser_sessions.set_persistent_browser_session_browser_address(*args, **kwargs)

    async def update_persistent_browser_session_compute_cost(self, *args: Any, **kwargs: Any) -> Any:
        return await self.browser_sessions.update_persistent_browser_session_compute_cost(*args, **kwargs)

    async def mark_persistent_browser_session_deleted(self, *args: Any, **kwargs: Any) -> Any:
        return await self.browser_sessions.mark_persistent_browser_session_deleted(*args, **kwargs)

    async def occupy_persistent_browser_session(self, *args: Any, **kwargs: Any) -> Any:
        return await self.browser_sessions.occupy_persistent_browser_session(*args, **kwargs)

    async def release_persistent_browser_session(self, *args: Any, **kwargs: Any) -> Any:
        return await self.browser_sessions.release_persistent_browser_session(*args, **kwargs)

    async def close_persistent_browser_session(self, *args: Any, **kwargs: Any) -> Any:
        return await self.browser_sessions.close_persistent_browser_session(*args, **kwargs)

    async def get_all_active_persistent_browser_sessions(self) -> list[PersistentBrowserSessionModel]:
        return await self.browser_sessions.get_all_active_persistent_browser_sessions()

    async def archive_browser_session_address(self, *args: Any, **kwargs: Any) -> Any:
        return await self.browser_sessions.archive_browser_session_address(*args, **kwargs)

    async def get_uncompleted_persistent_browser_sessions(self) -> list[PersistentBrowserSessionModel]:
        return await self.browser_sessions.get_uncompleted_persistent_browser_sessions()

    async def get_debug_session_by_browser_session_id(self, *args: Any, **kwargs: Any) -> Any:
        return await self.debug.get_debug_session_by_browser_session_id(*args, **kwargs)

    # -- Schedule delegates --

    async def create_workflow_schedule(self, *args: Any, **kwargs: Any) -> Any:
        return await self.schedules.create_workflow_schedule(*args, **kwargs)

    async def create_workflow_schedule_with_limit(self, *args: Any, **kwargs: Any) -> Any:
        return await self.schedules.create_workflow_schedule_with_limit(*args, **kwargs)

    async def set_backend_schedule_id(self, *args: Any, **kwargs: Any) -> Any:
        return await self.schedules.set_backend_schedule_id(*args, **kwargs)

    async def update_workflow_schedule(self, *args: Any, **kwargs: Any) -> Any:
        return await self.schedules.update_workflow_schedule(*args, **kwargs)

    async def get_workflow_schedule_by_id(self, *args: Any, **kwargs: Any) -> Any:
        return await self.schedules.get_workflow_schedule_by_id(*args, **kwargs)

    async def get_workflow_schedules(self, *args: Any, **kwargs: Any) -> Any:
        return await self.schedules.get_workflow_schedules(*args, **kwargs)

    async def get_all_enabled_schedules(self, *args: Any, **kwargs: Any) -> Any:
        return await self.schedules.get_all_enabled_schedules(*args, **kwargs)

    async def has_schedule_fired_since(self, *args: Any, **kwargs: Any) -> Any:
        return await self.schedules.has_schedule_fired_since(*args, **kwargs)

    async def update_workflow_schedule_enabled(self, *args: Any, **kwargs: Any) -> Any:
        return await self.schedules.update_workflow_schedule_enabled(*args, **kwargs)

    async def delete_workflow_schedule(self, *args: Any, **kwargs: Any) -> Any:
        return await self.schedules.delete_workflow_schedule(*args, **kwargs)

    async def restore_workflow_schedule(self, *args: Any, **kwargs: Any) -> Any:
        return await self.schedules.restore_workflow_schedule(*args, **kwargs)

    async def count_workflow_schedules(self, *args: Any, **kwargs: Any) -> Any:
        return await self.schedules.count_workflow_schedules(*args, **kwargs)

    async def list_organization_schedules(self, *args: Any, **kwargs: Any) -> Any:
        return await self.schedules.list_organization_schedules(*args, **kwargs)

    async def soft_delete_orphaned_schedules(self, *args: Any, **kwargs: Any) -> Any:
        return await self.schedules.soft_delete_orphaned_schedules(*args, **kwargs)

    # -- Script delegates --

    async def create_script(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.create_script(*args, **kwargs)

    async def get_scripts(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.get_scripts(*args, **kwargs)

    async def get_script(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.get_script(*args, **kwargs)

    async def get_script_revision(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.get_script_revision(*args, **kwargs)

    async def get_latest_script_version(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.get_latest_script_version(*args, **kwargs)

    async def get_script_versions(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.get_script_versions(*args, **kwargs)

    async def get_script_version_stats(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.get_script_version_stats(*args, **kwargs)

    async def soft_delete_script_by_revision(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.soft_delete_script_by_revision(*args, **kwargs)

    async def create_script_file(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.create_script_file(*args, **kwargs)

    async def create_script_block(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.create_script_block(*args, **kwargs)

    async def update_script_block(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.update_script_block(*args, **kwargs)

    async def get_script_files(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.get_script_files(*args, **kwargs)

    async def get_script_file_by_id(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.get_script_file_by_id(*args, **kwargs)

    async def get_script_file_by_path(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.get_script_file_by_path(*args, **kwargs)

    async def get_script_file_by_content_hash(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.get_script_file_by_content_hash(*args, **kwargs)

    async def update_script_file(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.update_script_file(*args, **kwargs)

    async def get_script_block(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.get_script_block(*args, **kwargs)

    async def get_script_block_by_label(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.get_script_block_by_label(*args, **kwargs)

    async def get_script_blocks_by_script_revision_id(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.get_script_blocks_by_script_revision_id(*args, **kwargs)

    async def create_workflow_script(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.create_workflow_script(*args, **kwargs)

    async def get_workflow_script(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.get_workflow_script(*args, **kwargs)

    async def get_workflow_script_by_cache_key_value(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.get_workflow_script_by_cache_key_value(*args, **kwargs)

    async def get_workflow_cache_key_count(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.get_workflow_cache_key_count(*args, **kwargs)

    async def get_workflow_cache_key_values(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.get_workflow_cache_key_values(*args, **kwargs)

    async def delete_workflow_cache_key_value(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.delete_workflow_cache_key_value(*args, **kwargs)

    async def delete_workflow_scripts_by_permanent_id(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.delete_workflow_scripts_by_permanent_id(*args, **kwargs)

    async def get_workflow_scripts_by_permanent_id(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.get_workflow_scripts_by_permanent_id(*args, **kwargs)

    async def get_workflow_runs_for_script(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.get_workflow_runs_for_script(*args, **kwargs)

    async def get_script_run_stats(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.get_script_run_stats(*args, **kwargs)

    async def is_script_pinned(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.is_script_pinned(*args, **kwargs)

    async def pin_workflow_script(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.pin_workflow_script(*args, **kwargs)

    async def unpin_workflow_script(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.unpin_workflow_script(*args, **kwargs)

    async def create_fallback_episode(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.create_fallback_episode(*args, **kwargs)

    async def get_unreviewed_episodes(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.get_unreviewed_episodes(*args, **kwargs)

    async def update_fallback_episode(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.update_fallback_episode(*args, **kwargs)

    async def delete_fallback_episode(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.delete_fallback_episode(*args, **kwargs)

    async def get_fallback_episodes(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.get_fallback_episodes(*args, **kwargs)

    async def get_fallback_episodes_count(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.get_fallback_episodes_count(*args, **kwargs)

    async def get_fallback_episode(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.get_fallback_episode(*args, **kwargs)

    async def mark_episode_reviewed(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.mark_episode_reviewed(*args, **kwargs)

    async def get_recent_reviewed_episodes(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.get_recent_reviewed_episodes(*args, **kwargs)

    async def record_branch_hit(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.record_branch_hit(*args, **kwargs)

    async def get_stale_branches(self, *args: Any, **kwargs: Any) -> Any:
        return await self.scripts.get_stale_branches(*args, **kwargs)

    # -- Observer delegates --

    async def get_task_v2(self, *args: Any, **kwargs: Any) -> Any:
        return await self.observer.get_task_v2(*args, **kwargs)

    async def delete_thoughts(self, *args: Any, **kwargs: Any) -> Any:
        return await self.observer.delete_thoughts(*args, **kwargs)

    async def get_task_v2_by_workflow_run_id(self, *args: Any, **kwargs: Any) -> Any:
        return await self.observer.get_task_v2_by_workflow_run_id(*args, **kwargs)

    async def get_thought(self, *args: Any, **kwargs: Any) -> Any:
        return await self.observer.get_thought(*args, **kwargs)

    async def get_thoughts(self, *args: Any, **kwargs: Any) -> Any:
        return await self.observer.get_thoughts(*args, **kwargs)

    async def create_task_v2(self, *args: Any, **kwargs: Any) -> Any:
        return await self.observer.create_task_v2(*args, **kwargs)

    async def create_thought(self, *args: Any, **kwargs: Any) -> Any:
        return await self.observer.create_thought(*args, **kwargs)

    async def update_thought(self, *args: Any, **kwargs: Any) -> Any:
        return await self.observer.update_thought(*args, **kwargs)

    async def update_task_v2(self, *args: Any, **kwargs: Any) -> Any:
        return await self.observer.update_task_v2(*args, **kwargs)

    async def create_workflow_run_block(self, *args: Any, **kwargs: Any) -> Any:
        return await self.observer.create_workflow_run_block(*args, **kwargs)

    async def delete_workflow_run_blocks(self, *args: Any, **kwargs: Any) -> Any:
        return await self.observer.delete_workflow_run_blocks(*args, **kwargs)

    async def update_workflow_run_block(self, *args: Any, **kwargs: Any) -> Any:
        return await self.observer.update_workflow_run_block(*args, **kwargs)

    async def get_workflow_run_block(self, *args: Any, **kwargs: Any) -> Any:
        return await self.observer.get_workflow_run_block(*args, **kwargs)

    async def get_workflow_run_block_by_task_id(self, *args: Any, **kwargs: Any) -> Any:
        return await self.observer.get_workflow_run_block_by_task_id(*args, **kwargs)

    async def get_workflow_run_blocks(self, *args: Any, **kwargs: Any) -> Any:
        return await self.observer.get_workflow_run_blocks(*args, **kwargs)

    # -- Artifact delegates --

    async def get_artifact_by_id_no_org(self, *args: Any, **kwargs: Any) -> Any:
        return await self.artifacts.get_artifact_by_id_no_org(*args, **kwargs)
