"""Tests for MCP auth state persistence tools (state_save / state_load)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.cli.core.browser_ops import _cookie_domain_matches
from skyvern.cli.core.result import BrowserContext
from skyvern.cli.core.session_manager import SessionState
from skyvern.cli.mcp_tools import state as mcp_state
from skyvern.cli.mcp_tools.state import _validate_state_path

# ═══════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════


def _make_mock_page(url: str = "https://example.com", title: str = "Example") -> MagicMock:
    page = MagicMock()
    page.url = url
    page.title = AsyncMock(return_value=title)
    page.evaluate = AsyncMock(return_value={})
    page.is_closed.return_value = False
    return page


def _make_mock_browser(cookies: list | None = None) -> MagicMock:
    browser = MagicMock()
    browser._browser_context = MagicMock()
    browser._browser_context.cookies = AsyncMock(return_value=cookies or [])
    browser._browser_context.add_cookies = AsyncMock()
    return browser


def _make_session_state(browser: MagicMock | None = None) -> SessionState:
    state = SessionState()
    state.browser = browser
    return state


def _patch_get_page(monkeypatch: pytest.MonkeyPatch, page: MagicMock, ctx: BrowserContext) -> AsyncMock:
    skyvern_page = SimpleNamespace(page=page)
    mock = AsyncMock(return_value=(skyvern_page, ctx))
    monkeypatch.setattr(mcp_state, "get_page", mock)
    return mock


def _patch_session(monkeypatch: pytest.MonkeyPatch, state: SessionState) -> MagicMock:
    mock = MagicMock(return_value=state)
    monkeypatch.setattr(mcp_state, "get_current_session", mock)
    return mock


# ═══════════════════════════════════════════════════
# _cookie_domain_matches
# ═══════════════════════════════════════════════════


class TestCookieDomainMatches:
    def test_exact_match(self) -> None:
        assert _cookie_domain_matches("example.com", "example.com") is True

    def test_subdomain_match_with_dot(self) -> None:
        assert _cookie_domain_matches(".example.com", "sub.example.com") is True

    def test_subdomain_match_without_dot(self) -> None:
        assert _cookie_domain_matches("example.com", "sub.example.com") is True

    def test_suffix_attack_rejected(self) -> None:
        assert _cookie_domain_matches("example.com", "evil-example.com") is False

    def test_empty_cookie_domain(self) -> None:
        assert _cookie_domain_matches("", "example.com") is False

    def test_empty_page_domain(self) -> None:
        assert _cookie_domain_matches("example.com", "") is False

    def test_both_empty(self) -> None:
        assert _cookie_domain_matches("", "") is False

    def test_dot_only_cookie_domain(self) -> None:
        assert _cookie_domain_matches(".", "example.com") is False

    def test_deep_subdomain_match(self) -> None:
        assert _cookie_domain_matches(".example.com", "a.b.c.example.com") is True

    def test_different_domain_rejected(self) -> None:
        assert _cookie_domain_matches("other.com", "example.com") is False


# ═══════════════════════════════════════════════════
# _validate_state_path
# ═══════════════════════════════════════════════════


class TestValidateStatePath:
    def test_valid_path_in_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        result = _validate_state_path("state.json")
        assert result == (tmp_path / "state.json").resolve()

    def test_valid_path_no_extension(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        result = _validate_state_path("mystate")
        assert result == (tmp_path / "mystate").resolve()

    def test_rejects_outside_allowed_roots(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ValueError, match="must be under working directory"):
            _validate_state_path("/etc/passwd")

    def test_rejects_path_traversal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ValueError, match="must be under working directory"):
            _validate_state_path("../../../etc/passwd")

    def test_rejects_symlinks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "real.json"
        target.write_text("{}")
        link = tmp_path / "link.json"
        link.symlink_to(target)
        with pytest.raises(ValueError, match="Symlinks not allowed"):
            _validate_state_path("link.json")

    def test_rejects_bad_extension(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ValueError, match="must have .json extension"):
            _validate_state_path("state.exe")

    def test_must_exist_raises_when_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        with pytest.raises(FileNotFoundError, match="State file not found"):
            _validate_state_path("missing.json", must_exist=True)

    def test_must_exist_passes_when_present(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        f = tmp_path / "exists.json"
        f.write_text("{}")
        result = _validate_state_path("exists.json", must_exist=True)
        assert result == f.resolve()

    def test_home_skyvern_path_allowed(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        skyvern_dir = tmp_path / ".skyvern"
        skyvern_dir.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.chdir(tmp_path / "elsewhere" if (tmp_path / "elsewhere").exists() else tmp_path)
        result = _validate_state_path(str(skyvern_dir / "state.json"))
        assert ".skyvern" in str(result)


# ═══════════════════════════════════════════════════
# skyvern_state_save
# ═══════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_state_save_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    cookies = [{"name": "sid", "value": "abc", "domain": "example.com", "path": "/"}]
    local_storage = {"key1": "val1"}
    session_storage = {"skey": "sval"}

    page = _make_mock_page("https://example.com")
    page.evaluate = AsyncMock(side_effect=[local_storage, session_storage])

    browser = _make_mock_browser(cookies)
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)
    _patch_session(monkeypatch, _make_session_state(browser))

    result = await mcp_state.skyvern_state_save(file_path="auth.json")

    assert result["ok"] is True
    assert result["data"]["cookie_count"] == 1
    assert result["data"]["local_storage_count"] == 1
    assert result["data"]["session_storage_count"] == 1

    saved = json.loads((tmp_path / "auth.json").read_text())
    assert saved["version"] == 1
    assert saved["cookies"] == cookies
    assert saved["local_storage"] == local_storage
    assert saved["session_storage"] == session_storage


@pytest.mark.asyncio
async def test_state_save_no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools._session import BrowserNotAvailableError

    monkeypatch.setattr(mcp_state, "get_page", AsyncMock(side_effect=BrowserNotAvailableError()))
    result = await mcp_state.skyvern_state_save(file_path="test.json")
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_state_save_invalid_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    page = _make_mock_page()
    browser = _make_mock_browser()
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)
    _patch_session(monkeypatch, _make_session_state(browser))

    result = await mcp_state.skyvern_state_save(file_path="/etc/evil.json")
    assert result["ok"] is False
    assert "must be under" in result["error"]["message"]


@pytest.mark.asyncio
async def test_state_save_no_browser_in_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    page = _make_mock_page()
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)
    _patch_session(monkeypatch, _make_session_state(None))

    result = await mcp_state.skyvern_state_save(file_path="auth.json")
    assert result["ok"] is False


# ═══════════════════════════════════════════════════
# skyvern_state_load
# ═══════════════════════════════════════════════════


def _write_state_file(path: Path, *, cookies: list | None = None, url: str = "https://example.com") -> None:
    state = {
        "version": 1,
        "url": url,
        "timestamp": "2026-04-01T00:00:00+00:00",
        "cookies": cookies or [],
        "local_storage": {"lk": "lv"},
        "session_storage": {"sk": "sv"},
    }
    path.write_text(json.dumps(state))


@pytest.mark.asyncio
async def test_state_load_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    cookies = [
        {"name": "sid", "value": "abc", "domain": "example.com", "path": "/"},
        {"name": "other", "value": "xyz", "domain": "evil.com", "path": "/"},
    ]
    state_file = tmp_path / "auth.json"
    _write_state_file(state_file, cookies=cookies)

    page = _make_mock_page("https://example.com")
    browser = _make_mock_browser()
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)
    _patch_session(monkeypatch, _make_session_state(browser))

    result = await mcp_state.skyvern_state_load(file_path="auth.json")

    assert result["ok"] is True
    assert result["data"]["cookie_count"] == 1
    assert result["data"]["skipped_cookies"] == 1
    assert result["data"]["local_storage_count"] == 1
    assert result["data"]["session_storage_count"] == 1

    browser._browser_context.add_cookies.assert_awaited_once()
    added = browser._browser_context.add_cookies.call_args[0][0]
    assert len(added) == 1
    assert added[0]["domain"] == "example.com"


@pytest.mark.asyncio
async def test_state_load_no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli.mcp_tools._session import BrowserNotAvailableError

    monkeypatch.setattr(mcp_state, "get_page", AsyncMock(side_effect=BrowserNotAvailableError()))
    result = await mcp_state.skyvern_state_load(file_path="test.json")
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_state_load_file_not_found(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    page = _make_mock_page()
    browser = _make_mock_browser()
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)
    _patch_session(monkeypatch, _make_session_state(browser))

    result = await mcp_state.skyvern_state_load(file_path="nonexistent.json")
    assert result["ok"] is False
    assert "not found" in result["error"]["message"].lower()


@pytest.mark.asyncio
async def test_state_load_bad_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    state_file = tmp_path / "bad.json"
    state_file.write_text(json.dumps({"version": 999}))

    page = _make_mock_page()
    browser = _make_mock_browser()
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)
    _patch_session(monkeypatch, _make_session_state(browser))

    result = await mcp_state.skyvern_state_load(file_path="bad.json")
    assert result["ok"] is False
    assert "version" in result["error"]["message"].lower()


@pytest.mark.asyncio
async def test_state_load_malformed_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    state_file = tmp_path / "bad.json"
    state_file.write_text("not json at all")

    page = _make_mock_page()
    browser = _make_mock_browser()
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)
    _patch_session(monkeypatch, _make_session_state(browser))

    result = await mcp_state.skyvern_state_load(file_path="bad.json")
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_state_load_filters_cross_domain_cookies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cookies from a different domain must not be applied."""
    monkeypatch.chdir(tmp_path)

    cookies = [
        {"name": "c1", "value": "v1", "domain": ".other.com", "path": "/"},
        {"name": "c2", "value": "v2", "domain": "another.org", "path": "/"},
    ]
    state_file = tmp_path / "cross.json"
    _write_state_file(state_file, cookies=cookies, url="https://other.com")

    page = _make_mock_page("https://example.com")
    browser = _make_mock_browser()
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)
    _patch_session(monkeypatch, _make_session_state(browser))

    result = await mcp_state.skyvern_state_load(file_path="cross.json")

    assert result["ok"] is True
    assert result["data"]["cookie_count"] == 0
    assert result["data"]["skipped_cookies"] == 2
    browser._browser_context.add_cookies.assert_not_awaited()


@pytest.mark.asyncio
async def test_state_save_load_roundtrip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Save then load should produce consistent results."""
    monkeypatch.chdir(tmp_path)

    cookies = [{"name": "tok", "value": "123", "domain": "example.com", "path": "/"}]
    ls = {"theme": "dark"}
    ss = {"cart": "item1"}

    page = _make_mock_page("https://example.com")
    page.evaluate = AsyncMock(side_effect=[ls, ss])

    browser = _make_mock_browser(cookies)
    ctx = BrowserContext(mode="local")
    _patch_get_page(monkeypatch, page, ctx)
    _patch_session(monkeypatch, _make_session_state(browser))

    save_result = await mcp_state.skyvern_state_save(file_path="roundtrip.json")
    assert save_result["ok"] is True

    page.evaluate = AsyncMock(return_value=None)
    load_result = await mcp_state.skyvern_state_load(file_path="roundtrip.json")
    assert load_result["ok"] is True
    assert load_result["data"]["cookie_count"] == 1
    assert load_result["data"]["local_storage_count"] == 1
    assert load_result["data"]["session_storage_count"] == 1
    assert load_result["data"]["skipped_cookies"] == 0
