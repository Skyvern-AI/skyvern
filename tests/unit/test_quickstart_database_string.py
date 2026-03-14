from __future__ import annotations

import typer
import pytest

from skyvern.cli import quickstart as quickstart_cmd


class _DummyStatus:
    def __enter__(self) -> "_DummyStatus":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def update(self, *_args, **_kwargs) -> None:
        return None


def _patch_console(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(quickstart_cmd.console, "print", lambda *a, **k: None)
    monkeypatch.setattr(quickstart_cmd.console, "status", lambda *a, **k: _DummyStatus())


def test_quickstart_non_string_database_string_still_requires_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_console(monkeypatch)
    monkeypatch.setattr(quickstart_cmd, "check_docker", lambda: False)

    init_called = False

    def _init_env(**_kwargs):
        nonlocal init_called
        init_called = True
        return True

    monkeypatch.setattr(quickstart_cmd, "init_env", _init_env)

    option_default = typer.Option("", "--database-string")

    with pytest.raises(typer.Exit) as exc:
        quickstart_cmd.quickstart(ctx=None, database_string=option_default, skip_browser_install=True)

    assert exc.value.exit_code == 1
    assert init_called is False


def test_quickstart_string_database_string_skips_docker_requirement(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_console(monkeypatch)
    monkeypatch.setattr(quickstart_cmd, "check_docker", lambda: False)
    monkeypatch.setattr(quickstart_cmd, "check_docker_compose_file", lambda: False)
    monkeypatch.setattr(quickstart_cmd, "start_services", lambda **_kwargs: None)

    seen: dict[str, object] = {}

    def _init_env(**kwargs):
        seen.update(kwargs)
        return False

    monkeypatch.setattr(quickstart_cmd, "init_env", _init_env)

    quickstart_cmd.quickstart(
        ctx=None,
        database_string="postgresql+psycopg://user:pass@localhost:5432/skyvern",
        skip_browser_install=True,
    )

    assert seen["database_string"] == "postgresql+psycopg://user:pass@localhost:5432/skyvern"
