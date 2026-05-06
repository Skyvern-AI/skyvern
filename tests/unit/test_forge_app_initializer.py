import runpy
import sys
from threading import Lock
from types import SimpleNamespace

import pytest

from skyvern.forge import forge_app_initializer


def test_start_forge_app_configures_server_logging_once(monkeypatch: pytest.MonkeyPatch) -> None:
    logging_calls: list[str] = []
    forced_app_instances: list[object] = []
    fake_app = object()

    monkeypatch.setattr(forge_app_initializer, "_SERVER_LOGGING_CONFIGURED", False)
    monkeypatch.setattr(forge_app_initializer, "_SERVER_LOGGING_LOCK", Lock())
    monkeypatch.setattr(forge_app_initializer, "create_forge_app", lambda: fake_app)
    monkeypatch.setattr(forge_app_initializer, "set_force_app_instance", forced_app_instances.append)
    monkeypatch.setattr(forge_app_initializer.settings, "ADDITIONAL_MODULES", [])

    import skyvern.forge.sdk.forge_log as forge_log

    monkeypatch.setattr(forge_log, "setup_logger", lambda: logging_calls.append("setup"))

    assert forge_app_initializer.start_forge_app() is fake_app
    assert forge_app_initializer.start_forge_app() is fake_app

    assert logging_calls == ["setup"]
    assert forced_app_instances == [fake_app, fake_app]


def test_forge_main_uses_shared_logging_initializer(monkeypatch: pytest.MonkeyPatch) -> None:
    logging_calls: list[str] = []
    uvicorn_calls: list[dict[str, object]] = []

    monkeypatch.setattr(forge_app_initializer, "_SERVER_LOGGING_CONFIGURED", False)
    monkeypatch.setattr(forge_app_initializer, "_SERVER_LOGGING_LOCK", Lock())

    import skyvern.forge.sdk.forge_log as forge_log

    monkeypatch.setattr(forge_log, "setup_logger", lambda: logging_calls.append("setup"))
    monkeypatch.setattr("skyvern.exceptions.require_server_extra_modules", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("skyvern.analytics.capture", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        SimpleNamespace(run=lambda *args, **kwargs: uvicorn_calls.append({"args": args, "kwargs": kwargs})),
    )

    runpy.run_module("skyvern.forge.__main__", run_name="__main__")
    forge_app_initializer._ensure_server_logging_configured()

    assert logging_calls == ["setup"]
    assert len(uvicorn_calls) == 1
