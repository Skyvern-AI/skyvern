from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from skyvern.forge.sdk.db import agent_db


def _fake_engine() -> AsyncEngine:
    engine = MagicMock(spec=AsyncEngine)
    engine.dialect = MagicMock()
    engine.dialect.name = "postgresql"
    engine.pool = MagicMock()
    return engine


def _call_build_engine(url: str = "postgresql+psycopg://user:pass@host/db") -> dict:
    captured: dict = {}

    def _capture(_url: str, **kwargs: object) -> AsyncEngine:
        captured.update(kwargs)
        return _fake_engine()

    with patch("skyvern.forge.sdk.db.agent_db.create_async_engine", side_effect=_capture):
        agent_db._build_engine(url)

    return captured


def test_pool_timeout_forwarded_to_queue_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    import skyvern.config

    monkeypatch.setattr(skyvern.config.settings, "DISABLE_CONNECTION_POOL", False)
    monkeypatch.setattr(skyvern.config.settings, "DATABASE_POOL_TIMEOUT", 7)

    captured = _call_build_engine()

    assert captured.get("pool_timeout") == 7


def test_pool_recycle_forwarded_to_queue_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    import skyvern.config

    monkeypatch.setattr(skyvern.config.settings, "DISABLE_CONNECTION_POOL", False)
    monkeypatch.setattr(skyvern.config.settings, "DATABASE_POOL_RECYCLE", 900)

    captured = _call_build_engine()

    assert captured.get("pool_recycle") == 900


def test_null_pool_path_does_not_forward_queue_pool_params(monkeypatch: pytest.MonkeyPatch) -> None:
    import skyvern.config

    monkeypatch.setattr(skyvern.config.settings, "DISABLE_CONNECTION_POOL", True)

    captured = _call_build_engine()

    assert "pool_timeout" not in captured
    assert "pool_recycle" not in captured
