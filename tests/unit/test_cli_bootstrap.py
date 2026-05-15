from __future__ import annotations

import json
import logging
import subprocess
import sys
import types

import skyvern._cli_bootstrap as cli_bootstrap
from skyvern.utils.env_paths import BACKEND_ENV_INTENT_ENV_VAR, EnvIntent


def test_bootstrap_defaults_to_warning_without_explicit_log_level(monkeypatch) -> None:
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    logger_names = ("", "skyvern", "httpx", "litellm", "playwright", "httpcore")
    previous_levels = {name: logging.getLogger(name).level for name in logger_names}
    try:
        cli_bootstrap.configure_cli_bootstrap_logging()
        assert "LOG_LEVEL" not in cli_bootstrap.os.environ
        for name in logger_names:
            assert logging.getLogger(name).level == logging.WARNING
    finally:
        for name, level in previous_levels.items():
            logging.getLogger(name).setLevel(level)


def test_bootstrap_honors_explicit_log_level(monkeypatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    logger_names = ("", "skyvern", "httpx", "litellm", "playwright", "httpcore")
    previous_levels = {name: logging.getLogger(name).level for name in logger_names}
    try:
        cli_bootstrap.configure_cli_bootstrap_logging()
        assert cli_bootstrap.os.environ["LOG_LEVEL"] == "DEBUG"
        for name in logger_names:
            assert logging.getLogger(name).level == logging.DEBUG
    finally:
        for name, level in previous_levels.items():
            logging.getLogger(name).setLevel(level)


def test_bootstrap_logging_does_not_import_settings() -> None:
    script = """
import json
import sys
from skyvern._cli_bootstrap import configure_cli_bootstrap_logging
before = "skyvern.config" in sys.modules
configure_cli_bootstrap_logging()
after = "skyvern.config" in sys.modules
print(json.dumps({"before": before, "after": after}))
"""
    result = subprocess.run([sys.executable, "-c", script], text=True, capture_output=True, check=True)

    assert json.loads(result.stdout) == {"before": False, "after": False}


def test_runtime_logging_lazily_calls_setup_logger(monkeypatch) -> None:
    calls: list[str] = []
    fake_forge_log = types.ModuleType("skyvern.forge.sdk.forge_log")

    def fake_setup_logger() -> None:
        calls.append("setup")

    fake_forge_log.setup_logger = fake_setup_logger

    monkeypatch.setattr(cli_bootstrap, "_RUNTIME_LOGGING_CONFIGURED", False)
    monkeypatch.setitem(sys.modules, "skyvern.forge.sdk.forge_log", fake_forge_log)

    cli_bootstrap.configure_cli_runtime_logging()
    cli_bootstrap.configure_cli_runtime_logging()

    assert calls == ["setup"]


def test_prepare_cli_runtime_loads_env_before_logger(monkeypatch, tmp_path) -> None:
    calls: list[str] = []
    project_env = tmp_path / ".skyvern" / ".env"
    project_env.parent.mkdir(parents=True)
    project_env.write_text("SKYVERN_API_KEY=project-key\n")

    fake_forge_log = types.ModuleType("skyvern.forge.sdk.forge_log")

    def fake_setup_logger() -> None:
        assert cli_bootstrap.os.environ[BACKEND_ENV_INTENT_ENV_VAR] == EnvIntent.CLOUD.value
        assert cli_bootstrap.os.environ["SKYVERN_API_KEY"] == "project-key"
        calls.append("setup")

    fake_forge_log.setup_logger = fake_setup_logger

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SKYVERN_API_KEY", raising=False)
    monkeypatch.delenv(BACKEND_ENV_INTENT_ENV_VAR, raising=False)
    monkeypatch.setattr(cli_bootstrap, "_RUNTIME_LOGGING_CONFIGURED", False)
    monkeypatch.setitem(sys.modules, "skyvern.forge.sdk.forge_log", fake_forge_log)

    assert cli_bootstrap.prepare_cli_runtime(intent=EnvIntent.CLOUD) == project_env
    assert calls == ["setup"]
