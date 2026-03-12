from __future__ import annotations

from pathlib import Path

import pytest
import typer

from skyvern.cli import init_command as init_cmd


@pytest.fixture
def patched_init_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, object]:
    """Patch side-effectful init dependencies and capture calls for assertions."""

    prompts = iter(["local", ""])
    monkeypatch.setattr(init_cmd.Prompt, "ask", lambda *args, **kwargs: next(prompts))
    monkeypatch.setattr(init_cmd.Confirm, "ask", lambda *args, **kwargs: False)

    calls: dict[str, object] = {
        "setup_postgresql": 0,
        "updates": [],
    }

    def _record_update(key: str, value: object) -> None:
        calls["updates"].append((key, value))

    monkeypatch.setattr(
        init_cmd,
        "setup_postgresql",
        lambda no_postgres=False: calls.__setitem__("setup_postgresql", int(calls["setup_postgresql"]) + 1),
    )
    monkeypatch.setattr(init_cmd, "update_or_add_env_var", _record_update)
    monkeypatch.setattr(init_cmd, "migrate_db", lambda: None)
    monkeypatch.setattr(init_cmd, "start_forge_app", lambda: None)

    async def _fake_setup_local_org() -> None:
        return None

    monkeypatch.setattr(init_cmd, "setup_local_organization", _fake_setup_local_org)
    monkeypatch.setattr(init_cmd, "resolve_backend_env_path", lambda: tmp_path / ".env")
    monkeypatch.setattr(init_cmd, "setup_llm_providers", lambda: None)
    monkeypatch.setattr(init_cmd, "setup_browser_config", lambda: ("chromium", None, None))
    monkeypatch.setattr(init_cmd, "setup_mcp", lambda local=False: None)
    monkeypatch.setattr(init_cmd, "capture_setup_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(init_cmd.subprocess, "run", lambda *args, **kwargs: None)

    return calls


def test_normalize_database_string_rejects_non_string() -> None:
    option_default = typer.Option("", "--database-string")
    assert init_cmd._normalize_database_string(option_default) == ""


def test_init_env_non_string_database_string_falls_back_to_postgres(
    patched_init_env: dict[str, object],
) -> None:
    option_default = typer.Option("", "--database-string")

    run_local = init_cmd.init_env(database_string=option_default)

    assert run_local is True
    assert patched_init_env["setup_postgresql"] == 1
    assert all(key != "DATABASE_STRING" for key, _ in patched_init_env["updates"])


def test_init_env_string_database_string_updates_env_and_skips_postgres(
    patched_init_env: dict[str, object],
) -> None:
    dsn = "postgresql+psycopg://user:pass@localhost:5432/skyvern"

    run_local = init_cmd.init_env(database_string=dsn)

    assert run_local is True
    assert patched_init_env["setup_postgresql"] == 0
    assert ("DATABASE_STRING", dsn) in patched_init_env["updates"]
