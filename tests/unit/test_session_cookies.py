from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import cast

import pytest
from playwright.async_api import BrowserContext

from skyvern.webeye.session_cookies import (
    SESSION_COOKIES_FILENAME,
    persist_session_cookies,
    restore_session_cookies,
)

_SESSION = {"name": "sess", "value": "a", "domain": "x.com", "path": "/", "expires": -1}
_SESSION_ZERO = {"name": "sess0", "value": "c", "domain": "x.com", "path": "/", "expires": 0}
_PERSISTENT = {"name": "persist", "value": "b", "domain": "x.com", "path": "/", "expires": 9999999999}


class FakeContext:
    def __init__(self, cookies: list[dict] | None = None) -> None:
        self._cookies = cookies or []
        self.added: list[list[dict]] = []

    async def cookies(self) -> list[dict]:
        return list(self._cookies)

    async def add_cookies(self, cookies: list[dict]) -> None:
        self.added.append(cookies)


class RaisingContext:
    async def cookies(self) -> list[dict]:
        raise RuntimeError("boom")

    async def add_cookies(self, cookies: list[dict]) -> None:
        raise RuntimeError("boom")


def _ctx(fake: object) -> BrowserContext:
    return cast(BrowserContext, fake)


def _sidecar(tmp_path: Path) -> Path:
    return tmp_path / SESSION_COOKIES_FILENAME


@pytest.mark.asyncio
async def test_persist_writes_only_session_cookies(tmp_path: Path) -> None:
    await persist_session_cookies(_ctx(FakeContext([_SESSION, _PERSISTENT])), str(tmp_path))
    written = json.loads(_sidecar(tmp_path).read_text())
    assert [c["name"] for c in written] == ["sess"]


@pytest.mark.asyncio
async def test_persist_treats_zero_expiry_as_session(tmp_path: Path) -> None:
    # patchright/stealth-chromium can report a session cookie's expiry as 0 instead of -1.
    await persist_session_cookies(_ctx(FakeContext([_SESSION_ZERO, _PERSISTENT])), str(tmp_path))
    written = json.loads(_sidecar(tmp_path).read_text())
    assert [c["name"] for c in written] == ["sess0"]


@pytest.mark.asyncio
async def test_persist_owner_only_permissions(tmp_path: Path) -> None:
    await persist_session_cookies(_ctx(FakeContext([_SESSION])), str(tmp_path))
    assert stat.S_IMODE(_sidecar(tmp_path).stat().st_mode) & 0o077 == 0


@pytest.mark.asyncio
async def test_persist_removes_stale_sidecar_when_no_session_cookies(tmp_path: Path) -> None:
    _sidecar(tmp_path).write_text(json.dumps([_SESSION]))
    await persist_session_cookies(_ctx(FakeContext([_PERSISTENT])), str(tmp_path))
    assert not _sidecar(tmp_path).exists()


@pytest.mark.asyncio
async def test_persist_cleans_tmp_on_replace_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom)
    # persist is best-effort: it swallows the failure and must not leave an orphaned .tmp behind.
    await persist_session_cookies(_ctx(FakeContext([_SESSION])), str(tmp_path))

    assert not (tmp_path / f"{SESSION_COOKIES_FILENAME}.tmp").exists()
    assert not _sidecar(tmp_path).exists()


@pytest.mark.asyncio
async def test_restore_sanitizes_keys_and_filters_session_only(tmp_path: Path) -> None:
    _sidecar(tmp_path).write_text(json.dumps([{**_SESSION, "partitionKey": "drop"}, _PERSISTENT]))
    fake = FakeContext()
    await restore_session_cookies(_ctx(fake), str(tmp_path))
    assert len(fake.added) == 1
    assert [c["name"] for c in fake.added[0]] == ["sess"]
    assert "partitionKey" not in fake.added[0][0]


@pytest.mark.asyncio
async def test_restore_noop_without_sidecar(tmp_path: Path) -> None:
    fake = FakeContext()
    await restore_session_cookies(_ctx(fake), str(tmp_path))
    assert fake.added == []


@pytest.mark.asyncio
async def test_best_effort_never_raises(tmp_path: Path) -> None:
    await persist_session_cookies(None, str(tmp_path))
    await persist_session_cookies(_ctx(RaisingContext()), str(tmp_path))
    _sidecar(tmp_path).write_text(json.dumps([_SESSION]))
    await restore_session_cookies(_ctx(RaisingContext()), str(tmp_path))


@pytest.mark.asyncio
async def test_restore_tolerates_corrupt_sidecar(tmp_path: Path) -> None:
    _sidecar(tmp_path).write_text("{ not valid json")
    fake = FakeContext()
    await restore_session_cookies(_ctx(fake), str(tmp_path))
    assert fake.added == []
