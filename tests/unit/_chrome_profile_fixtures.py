"""Synthetic Chrome user-data-dir carrying an earlier completed download, for profile-load tests."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

DOWNLOAD_HISTORY_TABLES = ("downloads", "downloads_url_chains", "downloads_slices")


def write_profile_with_prior_download(profile_dir: Path) -> None:
    default_dir = profile_dir / "Default"
    (default_dir / "shared_proto_db" / "metadata").mkdir(parents=True)
    (default_dir / "shared_proto_db" / "000003.log").write_bytes(b"synthetic-download-db")
    (default_dir / "Cookies").write_bytes(b"synthetic-cookie-jar")
    with closing(sqlite3.connect(default_dir / "History")) as connection:
        connection.executescript(
            """
            CREATE TABLE urls (id INTEGER PRIMARY KEY, url LONGVARCHAR);
            CREATE TABLE downloads (id INTEGER PRIMARY KEY, guid VARCHAR, target_path LONGVARCHAR, state INTEGER);
            CREATE TABLE downloads_url_chains (id INTEGER, chain_index INTEGER, url LONGVARCHAR);
            CREATE TABLE downloads_slices (download_id INTEGER, offset INTEGER, received_bytes INTEGER);
            INSERT INTO urls VALUES (1, 'https://example.test/report');
            INSERT INTO downloads VALUES (1, 'guid-1', '/downloads/report.xlsx', 1);
            INSERT INTO downloads_url_chains VALUES (1, 0, 'https://example.test/report.xlsx');
            INSERT INTO downloads_slices VALUES (1, 0, 100);
            """
        )
        connection.commit()


def history_row_counts(profile_dir: Path) -> dict[str, int]:
    with closing(sqlite3.connect(profile_dir / "Default" / "History")) as connection:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("urls", *DOWNLOAD_HISTORY_TABLES)
        }
