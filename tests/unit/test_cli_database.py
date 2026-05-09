from __future__ import annotations

import subprocess

import pytest

from skyvern.cli import database


def test_create_database_and_user_treats_already_exists_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[0] == "createuser":
            return subprocess.CompletedProcess(
                args,
                1,
                stdout="",
                stderr='createuser: error: creation of new role failed: ERROR: role "skyvern" already exists',
            )
        if args[0] == "createdb":
            return subprocess.CompletedProcess(
                args,
                1,
                stdout="",
                stderr='createdb: error: database creation failed: ERROR: database "skyvern" already exists',
            )
        raise AssertionError(f"Unexpected command: {args}")

    monkeypatch.setattr(database, "_role_exists_via_catalog", lambda _user: False)
    monkeypatch.setattr(database, "_database_exists_via_catalog", lambda _dbname: False)
    monkeypatch.setattr(database.subprocess, "run", fake_run)

    database.create_database_and_user()

    assert calls == [["createuser", "skyvern"], ["createdb", "skyvern", "-O", "skyvern"]]


def test_missing_local_server_dependency_detects_fuzzysearch() -> None:
    from skyvern.cli.init_command import _missing_local_server_dependency

    assert _missing_local_server_dependency(ModuleNotFoundError("No module named fuzzysearch", name="fuzzysearch")) == (
        "fuzzysearch"
    )
