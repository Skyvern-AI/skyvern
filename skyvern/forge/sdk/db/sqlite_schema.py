from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from skyvern.forge.sdk.db.models import Base

# Columns that migrations added to tables an older local SQLite database already has. create_all only
# creates missing tables, so an upgraded install needs each of these added here.
_SQLITE_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("organizations", "slug", "VARCHAR"),
    ("tasks", "attempt_number", "INTEGER"),
    ("workflow_run_blocks", "attempt_number", "INTEGER"),
    ("workflow_run_blocks", "capped_output", "JSON"),
    ("workflow_run_blocks", "tool_calls", "JSON"),
    ("google_oauth_credentials", "google_subject", "VARCHAR"),
    ("tasks", "task_queue", "VARCHAR"),
)


def upgrade_sqlite_schema(connection: Connection) -> None:
    """Add the columns and indexes that create_all leaves out of an existing local SQLite database."""
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    for table, column, column_type in _SQLITE_ADDED_COLUMNS:
        if table in tables and column not in {existing["name"] for existing in inspector.get_columns(table)}:
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"))
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
