from __future__ import annotations

import os
import shutil
import sqlite3
from contextlib import closing
from typing import Any

import structlog

LOG = structlog.get_logger()

_CHROME_DOWNLOAD_HISTORY_TABLES = ("downloads", "downloads_url_chains", "downloads_slices")


def strip_download_history_state(session_dir: str, **log_kwargs: Any) -> None:
    """Drop a saved profile's record of earlier downloads before launching the browser with it. Chrome 152+ crashes
    the browser process on the next download when a completed download under 24 hours old is in both History and
    the shared_proto_db download DB; Cookies and the rest of History are untouched."""
    default_dir = os.path.join(session_dir, "Default")
    download_db_dir = os.path.join(default_dir, "shared_proto_db")
    removed_download_db = os.path.isdir(download_db_dir)
    if removed_download_db:
        shutil.rmtree(download_db_dir, ignore_errors=True)

    removed_rows = 0
    history_path = os.path.join(default_dir, "History")
    if os.path.isfile(history_path):
        try:
            with closing(sqlite3.connect(history_path)) as connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                for table in _CHROME_DOWNLOAD_HISTORY_TABLES:
                    if table in tables:
                        removed_rows += connection.execute(f"DELETE FROM {table}").rowcount
                connection.commit()
        except sqlite3.Error:
            LOG.warning("Failed to clear download history in reused browser profile", exc_info=True, **log_kwargs)

    if removed_download_db or removed_rows:
        LOG.info(
            "Stripped prior download state from reused browser profile",
            removed_download_db=removed_download_db,
            removed_download_history_rows=removed_rows,
            **log_kwargs,
        )
