"""Queries that need a SQLite-specific form, exercised on SQLite and compiled for Postgres.

SQLite is the default local database, but several repository queries used Postgres-only SQL:
GREATEST/LEAST, DISTINCT ON (silently dropped by SQLAlchemy on SQLite, returning every row),
JSONPATH, and unique indexes whose partial predicate existed only as postgresql_where. Each now
has a SQLite branch; the Postgres SQL must stay exactly as it was.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text, update
from sqlalchemy.dialects import postgresql, sqlite

from skyvern.forge.api_app import upgrade_sqlite_schema
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.copilot.ask_user import QuestionInteraction, QuestionPart
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import UploadedFileModel, WorkflowCopilotChatModel
from skyvern.forge.sdk.db.repositories.artifacts import ArtifactsRepository
from skyvern.forge.sdk.db.repositories.browser_sessions import BrowserSessionsRepository
from skyvern.forge.sdk.db.repositories.uploaded_files import UploadedFilesRepository
from skyvern.forge.sdk.db.repositories.workflow_parameters import WorkflowParametersRepository
from skyvern.forge.sdk.schemas.copilot_turn_outcome import ResponseKind, TurnOutcome
from skyvern.forge.sdk.schemas.workflow_copilot import CopilotPendingTurn, WorkflowCopilotChatSender

ORG = "o_sqlite_dialect"
T_OLD = datetime(2026, 10, 2, 11, 0, 0)
T_NEW = datetime(2026, 10, 2, 12, 0, 0)


def _naive(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=None) if value is not None else None


async def _sql(agent_db: AgentDB, statement: str, **params: Any) -> None:
    async with agent_db.engine.begin() as conn:
        await conn.execute(text(statement), params)


# --------------------------------------------------------------------------- GREATEST / LEAST


async def test_touch_last_activity_never_moves_backwards(agent_db: AgentDB) -> None:
    repo = agent_db.browser_sessions
    session = await repo.create_persistent_browser_session(ORG)
    sid = session.persistent_browser_session_id

    await repo.touch_last_activity(sid, T_OLD)  # NULL -> T_OLD
    await repo.touch_last_activity(sid, T_NEW)
    await repo.touch_last_activity(sid, T_OLD)  # late, out-of-order touch

    stored = await repo.get_persistent_browser_session(sid, ORG)
    assert stored is not None
    assert _naive(stored.last_activity_at) == T_NEW


async def test_attach_uploaded_files_only_moves_expiry_earlier(agent_db: AgentDB) -> None:
    repo = agent_db.uploaded_files
    far, near, later = T_NEW + timedelta(days=30), T_NEW + timedelta(days=1), T_NEW + timedelta(days=60)
    uploaded = await repo.create_uploaded_file("file_1", ORG, "s3://bucket/file_1", "a.txt", 1, expires_at=far)

    shortened = await repo.attach_uploaded_files_to_run([uploaded.file_id], ORG, "wr_1", near)
    kept = await repo.attach_uploaded_files_to_run([uploaded.file_id], ORG, "wr_1", later)

    assert [_naive(f.expires_at) for f in shortened] == [near]
    assert [_naive(f.expires_at) for f in kept] == [near]


# --------------------------------------------------------------------------- DISTINCT ON


async def test_latest_artifact_per_task_returns_one_artifact_per_task(agent_db: AgentDB) -> None:
    for artifact_id, task_id, created_at in [
        ("a_old", "t_1", T_OLD),
        ("a_new", "t_1", T_NEW),
        ("a_only", "t_2", T_OLD),
    ]:
        await agent_db.artifacts.create_artifact(
            artifact_id, ArtifactType.SCREENSHOT_FINAL, f"file:///{artifact_id}", ORG, task_id=task_id
        )
        await _sql(
            agent_db, "UPDATE artifacts SET created_at = :ts WHERE artifact_id = :id", ts=created_at, id=artifact_id
        )

    latest = await agent_db.artifacts.get_latest_artifact_per_task_ids(
        ["t_1", "t_2"], [ArtifactType.SCREENSHOT_FINAL], ORG
    )

    assert sorted(a.artifact_id for a in latest) == ["a_new", "a_only"]


async def test_copilot_chat_list_has_one_row_per_chat_titled_by_first_message(agent_db: AgentDB) -> None:
    params = agent_db.workflow_params
    chat = await params.create_workflow_copilot_chat(ORG, "wpid_list")
    for content in ("first message", "second message"):
        await params.create_workflow_copilot_chat_message(
            ORG, chat.workflow_copilot_chat_id, WorkflowCopilotChatSender.USER, content
        )

    chats = await params.get_workflow_copilot_chats(ORG, "wpid_list")

    assert len(chats) == 1
    assert chats[0].title == "first message"
    assert chats[0].awaiting_user_input is False


# --------------------------------------------------------------------------- JSONPATH


async def _set_pending_turns(agent_db: AgentDB, chat_id: str, pending_turns: dict[str, Any]) -> None:
    async with agent_db.Session() as session:
        await session.execute(
            update(WorkflowCopilotChatModel)
            .where(WorkflowCopilotChatModel.workflow_copilot_chat_id == chat_id)
            .values(pending_turns=pending_turns)
        )
        await session.commit()


def _turn(
    turn_id: str,
    *,
    cancel_token: str | None = None,
    question_status: str | None = None,
    heartbeat: datetime | None = None,
) -> dict[str, Any]:
    """A pending turn serialized exactly the way the repository stores one."""
    questions = []
    if question_status is not None:
        questions.append(
            QuestionInteraction(
                interaction_id=f"qi_{turn_id}",
                turn_id=turn_id,
                tool_call_id=f"tc_{turn_id}",
                parts=[QuestionPart(part_id="p_1", prompt="Which one?")],
                status=question_status,
            )
        )
    turn = CopilotPendingTurn(
        turn_id=turn_id,
        started_at=T_NEW,
        cancel_token=cancel_token,
        question_interactions=questions,
        question_heartbeat_at=heartbeat,
    )
    return turn.model_dump(mode="json")


async def test_latest_copilot_chat_found_by_pending_turn_cancel_token(agent_db: AgentDB) -> None:
    params = agent_db.workflow_params
    chat = await params.create_workflow_copilot_chat(ORG, "wpid_cancel")
    await _set_pending_turns(agent_db, chat.workflow_copilot_chat_id, {"turn_1": _turn("turn_1", cancel_token="tok")})

    found = await params.get_latest_workflow_copilot_chat(ORG, "wpid_cancel", request_cancel_token="tok")
    missing = await params.get_latest_workflow_copilot_chat(ORG, "wpid_cancel", request_cancel_token="other")

    assert found is not None and found.workflow_copilot_chat_id == chat.workflow_copilot_chat_id
    assert missing is None


async def test_latest_copilot_chat_found_by_completed_turn_cancel_token(agent_db: AgentDB) -> None:
    # The other half of the same OR: the token lives on a finished turn's outcome, not a pending turn.
    params = agent_db.workflow_params
    chat = await params.create_workflow_copilot_chat(ORG, "wpid_done")
    await params.create_workflow_copilot_chat_message(
        ORG,
        chat.workflow_copilot_chat_id,
        WorkflowCopilotChatSender.AI,
        "done",
        turn_outcome=TurnOutcome(response_kind=list(ResponseKind)[0], request_cancel_token="tok_done"),
    )

    found = await params.get_latest_workflow_copilot_chat(ORG, "wpid_done", request_cancel_token="tok_done")
    missing = await params.get_latest_workflow_copilot_chat(ORG, "wpid_done", request_cancel_token="other")

    assert found is not None and found.workflow_copilot_chat_id == chat.workflow_copilot_chat_id
    assert missing is None


async def test_copilot_chat_list_reports_live_pending_question(agent_db: AgentDB) -> None:
    params = agent_db.workflow_params
    chat = await params.create_workflow_copilot_chat(ORG, "wpid_question")
    await params.create_workflow_copilot_chat_message(
        ORG, chat.workflow_copilot_chat_id, WorkflowCopilotChatSender.USER, "hello"
    )
    now = datetime.now(timezone.utc)
    await _set_pending_turns(
        agent_db,
        chat.workflow_copilot_chat_id,
        {
            "resolved": _turn("resolved", question_status="resolved", heartbeat=now),
            "no_heartbeat": _turn("no_heartbeat", question_status="pending"),
            "waiting": _turn("waiting", question_status="pending", heartbeat=now),
        },
    )

    chats = await params.get_workflow_copilot_chats(ORG, "wpid_question")

    assert len(chats) == 1
    assert chats[0].awaiting_user_input is True


async def test_copilot_chat_list_ignores_heartbeats_of_resolved_questions(agent_db: AgentDB) -> None:
    params = agent_db.workflow_params
    chat = await params.create_workflow_copilot_chat(ORG, "wpid_resolved")
    await params.create_workflow_copilot_chat_message(
        ORG, chat.workflow_copilot_chat_id, WorkflowCopilotChatSender.USER, "hello"
    )
    now = datetime.now(timezone.utc)
    await _set_pending_turns(
        agent_db,
        chat.workflow_copilot_chat_id,
        {"resolved": _turn("resolved", question_status="resolved", heartbeat=now)},
    )

    chats = await params.get_workflow_copilot_chats(ORG, "wpid_resolved")

    assert len(chats) == 1
    assert chats[0].awaiting_user_input is False


# --------------------------------------------------------------------------- partial unique indexes


async def test_workflow_can_be_rebound_after_its_browser_session_closed(agent_db: AgentDB) -> None:
    repo = agent_db.browser_sessions
    first = await repo.create_persistent_browser_session(ORG, bound_workflow_permanent_id="wpid_b", bound_key="k")
    await repo.close_persistent_browser_session(first.persistent_browser_session_id, ORG)

    second = await repo.create_persistent_browser_session(ORG, bound_workflow_permanent_id="wpid_b", bound_key="k")

    assert second.persistent_browser_session_id != first.persistent_browser_session_id


async def test_storage_uri_can_be_reused_after_soft_delete(agent_db: AgentDB) -> None:
    repo = agent_db.uploaded_files
    first = await repo.create_uploaded_file("file_a", ORG, "s3://bucket/same", "a.txt", 1)
    async with agent_db.Session() as session:
        await session.execute(
            update(UploadedFileModel).where(UploadedFileModel.file_id == first.file_id).values(deleted_at=T_NEW)
        )
        await session.commit()

    second = await repo.create_uploaded_file("file_b", ORG, "s3://bucket/same", "a.txt", 1)

    assert second.file_id == "file_b"


@pytest.mark.parametrize(
    ("index_name", "table", "legacy_columns"),
    [
        (
            "uq_pbs_live_workflow_binding",
            "persistent_browser_sessions",
            "organization_id, bound_workflow_permanent_id, COALESCE(bound_key, '')",
        ),
        ("ux_uploaded_files_org_storage_uri_live", "uploaded_files", "organization_id, storage_uri"),
    ],
)
async def test_upgrade_rebuilds_legacy_full_unique_index_as_partial(
    agent_db: AgentDB, index_name: str, table: str, legacy_columns: str
) -> None:
    # Recreate the index the way databases bootstrapped before sqlite_where existed have it.
    await _sql(agent_db, f"DROP INDEX {index_name}")
    await _sql(agent_db, f"CREATE UNIQUE INDEX {index_name} ON {table} ({legacy_columns})")

    async with agent_db.engine.begin() as conn:
        await conn.run_sync(upgrade_sqlite_schema)
        ddl = (
            await conn.execute(
                text("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :name"), {"name": index_name}
            )
        ).scalar_one()

    assert " WHERE " in ddl.upper()


async def _schema(agent_db: AgentDB) -> list[tuple[str, str]]:
    async with agent_db.engine.connect() as conn:
        rows = await conn.execute(text("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"))
        return [tuple(row) for row in rows]


async def test_upgrade_leaves_current_schema_untouched(agent_db: AgentDB) -> None:
    # A database created from the current models already has every partial index; startup runs the
    # upgrade every time, so it must be a no-op here (and on its own second run).
    before = await _schema(agent_db)
    for _ in range(2):
        async with agent_db.engine.begin() as conn:
            await conn.run_sync(upgrade_sqlite_schema)

    assert await _schema(agent_db) == before


# --------------------------------------------------------------------------- Postgres SQL is unchanged


class _EmptyResult:
    def all(self) -> list:
        return []

    def first(self) -> None:
        return None

    def scalars(self) -> _EmptyResult:
        return self


class _RecordingSession:
    """Stands in for an AsyncSession: records statements instead of running them."""

    def __init__(self, dialect: Any) -> None:
        self.bind = SimpleNamespace(dialect=dialect)
        self.statements: list[Any] = []

    async def __aenter__(self) -> _RecordingSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> _EmptyResult:
        self.statements.append(statement)
        return _EmptyResult()

    async def scalars(self, statement: Any, *args: Any, **kwargs: Any) -> _EmptyResult:
        self.statements.append(statement)
        return _EmptyResult()

    async def commit(self) -> None:
        pass


async def _compiled_sql(dialect: Any, repo_cls: type, method: str, *args: Any) -> str:
    recorder = _RecordingSession(dialect)
    repo = repo_cls(lambda: recorder)
    await getattr(repo, method)(*args)
    return "\n".join(str(statement.compile(dialect=dialect)) for statement in recorder.statements)


# (repository, method, args, what Postgres must still get, what SQLite gets instead)
_CALLS = [
    (BrowserSessionsRepository, "touch_last_activity", ("pbs_1", T_NEW), "greatest(", "max("),
    (UploadedFilesRepository, "attach_uploaded_files_to_run", (["f_1"], ORG, "wr_1", T_NEW), "least(", "min("),
    (
        ArtifactsRepository,
        "get_latest_artifact_per_task_ids",
        (["t_1"], [ArtifactType.SCREENSHOT_FINAL], ORG),
        "DISTINCT ON",
        "row_number()",
    ),
    (
        WorkflowParametersRepository,
        "get_latest_workflow_copilot_chat",
        (ORG, "wpid", "tok"),
        "jsonb_path_exists(",
        "json_each(",
    ),
    (
        WorkflowParametersRepository,
        "get_workflow_copilot_chats",
        (ORG, "wpid"),
        "jsonb_path_query_array(",
        "json_group_array(",
    ),
]


@pytest.mark.parametrize(
    ("repo_cls", "method", "args", "postgres_form", "sqlite_form"), _CALLS, ids=[call[1] for call in _CALLS]
)
async def test_each_dialect_gets_its_own_form(
    repo_cls: type, method: str, args: tuple, postgres_form: str, sqlite_form: str
) -> None:
    pg_sql = await _compiled_sql(postgresql.dialect(), repo_cls, method, *args)
    sqlite_sql = await _compiled_sql(sqlite.dialect(), repo_cls, method, *args)

    assert postgres_form in pg_sql and sqlite_form not in pg_sql, pg_sql
    assert sqlite_form in sqlite_sql and postgres_form not in sqlite_sql, sqlite_sql
