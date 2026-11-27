from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from skyvern.forge.sdk.db.models import Base, TOTPCodeModel

# Columns that migrations added to tables an older local SQLite database already has. create_all only
# creates missing tables, so an upgraded install needs each of these added here.
_SQLITE_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("organizations", "slug", "VARCHAR"),
    ("tasks", "attempt_number", "INTEGER"),
    ("workflow_run_blocks", "attempt_number", "INTEGER"),
    ("workflow_run_blocks", "capped_output", "JSON"),
    ("workflow_run_blocks", "tool_calls", "JSON"),
    ("google_oauth_credentials", "google_subject", "VARCHAR"),
    ("google_oauth_credentials", "initiating_user_id", "VARCHAR"),
    ("tasks", "task_queue", "VARCHAR"),
    ("browser_profiles", "source_browser_session_id", "VARCHAR"),
    ("browser_profiles", "source_workflow_run_id", "VARCHAR"),
    ("artifacts", "download_filename", "VARCHAR"),
    ("totp_codes", "code_digest", "VARCHAR"),
    ("totp_codes", "fields_encrypted", "BOOLEAN"),
)
_SQLITE_ADDED_TOTP_INDEXES = frozenset({"uq_totp_codes_strict_code_digest", "uq_totp_codes_strict_webhook_code_digest"})


def upgrade_sqlite_schema(connection: Connection) -> None:
    """Add the columns and indexes that create_all leaves out of an existing local SQLite database."""
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    for table, column, column_type in _SQLITE_ADDED_COLUMNS:
        if table in tables and column not in {existing["name"] for existing in inspector.get_columns(table)}:
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"))
    if "totp_codes" in tables:
        for index in TOTPCodeModel.__table__.indexes:
            if index.name in _SQLITE_ADDED_TOTP_INDEXES:
                index.create(connection, checkfirst=True)
    if "organizations" in tables:
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_organizations_slug ON organizations (slug) WHERE slug IS NOT NULL"
            )
        )


async def ensure_sqlite_schema(engine: AsyncEngine) -> None:
    """Create missing tables and add missing columns to a local SQLite database."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(upgrade_sqlite_schema)


async def ensure_sqlite_schema_at(database_string: str) -> None:
    engine = create_async_engine(database_string)
    try:
        await ensure_sqlite_schema(engine)
    finally:
        await engine.dispose()
