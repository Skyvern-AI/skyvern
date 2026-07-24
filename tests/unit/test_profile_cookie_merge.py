from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import cast

import pytest
from playwright.async_api import BrowserContext

from skyvern.webeye.profile_cookie_merge import (
    BANKED_COOKIES_FILENAME,
    _cookie_key,
    clear_banked_cookies,
    cookie_delta,
    seed_cookie_values,
    union_cookies_into_profile_dir,
)
from skyvern.webeye.session_cookies import restore_banked_cookies

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
    async def add_cookies(self, cookies: list[dict]) -> None:
        raise RuntimeError("boom")


def _ctx(fake: object) -> BrowserContext:
    return cast(BrowserContext, fake)


def _sidecar(tmp_path: Path) -> Path:
    return tmp_path / BANKED_COOKIES_FILENAME


def _read(tmp_path: Path) -> list[dict]:
    return json.loads(_sidecar(tmp_path).read_text())


def test_union_writes_all_cookie_kinds(tmp_path: Path) -> None:
    # Unlike the session sidecar, the banked sidecar carries persistent cookies too.
    n = union_cookies_into_profile_dir([_SESSION, _PERSISTENT], str(tmp_path))
    assert n == 2
    assert {c["name"] for c in _read(tmp_path)} == {"sess", "persist"}


def test_union_incoming_wins_on_key_clash(tmp_path: Path) -> None:
    _sidecar(tmp_path).write_text(json.dumps([{**_SESSION, "value": "old"}]))
    union_cookies_into_profile_dir([{**_SESSION, "value": "new"}], str(tmp_path))
    written = _read(tmp_path)
    assert len(written) == 1 and written[0]["value"] == "new"


def test_union_preserves_disjoint_existing(tmp_path: Path) -> None:
    _sidecar(tmp_path).write_text(json.dumps([_PERSISTENT]))
    union_cookies_into_profile_dir([_SESSION], str(tmp_path))
    assert {c["name"] for c in _read(tmp_path)} == {"sess", "persist"}


def test_union_key_is_domain_name_path(tmp_path: Path) -> None:
    a = {"name": "k", "value": "1", "domain": "a.com", "path": "/"}
    b = {"name": "k", "value": "2", "domain": "b.com", "path": "/"}
    union_cookies_into_profile_dir([a, b], str(tmp_path))
    # Same name, different domain -> two distinct entries (never merged).
    assert len(_read(tmp_path)) == 2


def test_union_skips_cookies_missing_name_or_domain(tmp_path: Path) -> None:
    n = union_cookies_into_profile_dir([{"value": "x"}, {"name": "n"}, _SESSION], str(tmp_path))
    assert n == 1 and [c["name"] for c in _read(tmp_path)] == ["sess"]


def test_union_strips_unknown_keys(tmp_path: Path) -> None:
    union_cookies_into_profile_dir([{**_SESSION, "partitionKey": "drop"}], str(tmp_path))
    assert "partitionKey" not in _read(tmp_path)[0]


def test_union_empty_removes_sidecar(tmp_path: Path) -> None:
    _sidecar(tmp_path).write_text(json.dumps([_SESSION]))
    # Nothing valid incoming and (after discard) nothing existing survives -> remove file.
    assert union_cookies_into_profile_dir([{"value": "x"}], str(tmp_path)) == 1  # existing survives
    _sidecar(tmp_path).write_text(json.dumps([{"value": "bad"}]))
    assert union_cookies_into_profile_dir([], str(tmp_path)) == 0
    assert not _sidecar(tmp_path).exists()


def test_union_tolerates_corrupt_existing_sidecar(tmp_path: Path) -> None:
    _sidecar(tmp_path).write_text("{ not valid json")
    n = union_cookies_into_profile_dir([_SESSION], str(tmp_path))
    assert n == 1 and [c["name"] for c in _read(tmp_path)] == ["sess"]


def test_union_owner_only_permissions(tmp_path: Path) -> None:
    union_cookies_into_profile_dir([_SESSION], str(tmp_path))
    assert stat.S_IMODE(_sidecar(tmp_path).stat().st_mode) & 0o077 == 0


def test_union_noop_when_dir_missing(tmp_path: Path) -> None:
    assert union_cookies_into_profile_dir([_SESSION], str(tmp_path / "nope")) == 0


def test_clear_removes_sidecar(tmp_path: Path) -> None:
    _sidecar(tmp_path).write_text(json.dumps([_SESSION]))
    clear_banked_cookies(str(tmp_path))
    assert not _sidecar(tmp_path).exists()
    clear_banked_cookies(str(tmp_path))  # idempotent, no raise


@pytest.mark.asyncio
async def test_restore_injects_and_preserves_persistent_expiry(tmp_path: Path) -> None:
    _sidecar(tmp_path).write_text(json.dumps([_SESSION_ZERO, _PERSISTENT]))
    fake = FakeContext()
    await restore_banked_cookies(_ctx(fake), str(tmp_path))
    assert len(fake.added) == 1
    by_name = {c["name"]: c for c in fake.added[0]}
    assert by_name["sess0"]["expires"] == -1  # session expiry (0) pinned to -1
    assert by_name["persist"]["expires"] == 9999999999  # persistent expiry preserved


@pytest.mark.asyncio
async def test_restore_noop_without_sidecar(tmp_path: Path) -> None:
    fake = FakeContext()
    await restore_banked_cookies(_ctx(fake), str(tmp_path))
    assert fake.added == []


@pytest.mark.asyncio
async def test_restore_best_effort_never_raises(tmp_path: Path) -> None:
    await restore_banked_cookies(None, str(tmp_path))
    _sidecar(tmp_path).write_text(json.dumps([_PERSISTENT]))
    await restore_banked_cookies(_ctx(RaisingContext()), str(tmp_path))


def test_cookie_delta_new_and_changed_only() -> None:
    seed = [
        {"name": "a", "value": "1", "domain": "x.com", "path": "/"},
        {"name": "b", "value": "2", "domain": "x.com", "path": "/"},
    ]
    end_state = [
        {"name": "a", "value": "1", "domain": "x.com", "path": "/"},  # unchanged -> excluded
        {"name": "b", "value": "99", "domain": "x.com", "path": "/"},  # changed value -> included
        {"name": "c", "value": "3", "domain": "x.com", "path": "/"},  # new -> included
    ]
    delta = cookie_delta(end_state, seed)
    by_name = {c["name"]: c["value"] for c in delta}
    assert by_name == {"b": "99", "c": "3"}


def test_cookie_delta_ignores_deletions() -> None:
    # A cookie present in seed but gone from end-state is NOT propagated: a stale run must never remove
    # a cookie a concurrent fresher login relies on.
    seed = [{"name": "gone", "value": "1", "domain": "x.com", "path": "/"}]
    assert cookie_delta([], seed) == []


def test_cookie_delta_same_name_distinct_path_is_a_new_key() -> None:
    seed = [{"name": "a", "value": "1", "domain": "x.com", "path": "/"}]
    end_state = [{"name": "a", "value": "1", "domain": "x.com", "path": "/app"}]
    delta = cookie_delta(end_state, seed)
    assert len(delta) == 1 and delta[0]["path"] == "/app"


def _sidecar_values(profile_dir: Path) -> dict:
    data = json.loads((profile_dir / BANKED_COOKIES_FILENAME).read_text())
    return {_cookie_key(c): c["value"] for c in data}


def test_three_way_keeps_fresher_concurrent_write(tmp_path: Path) -> None:
    # Lawy's scenario: the profile's current sidecar already holds a concurrent fresher login's value
    # for key K (diverged from our seed). Our stale delta also changed K. The three-way must keep theirs.
    (tmp_path / BANKED_COOKIES_FILENAME).write_text(
        json.dumps([{"name": "sid", "value": "FRESHER", "domain": "x.com", "path": "/"}])
    )
    seed = [{"name": "sid", "value": "SEED", "domain": "x.com", "path": "/"}]
    ours_stale = [{"name": "sid", "value": "OURS_STALE", "domain": "x.com", "path": "/"}]
    union_cookies_into_profile_dir(ours_stale, str(tmp_path), base_values=seed_cookie_values(seed))
    assert _sidecar_values(tmp_path)[("x.com", "sid", "/")] == "FRESHER"  # theirs survives


def test_three_way_applies_new_key(tmp_path: Path) -> None:
    # A new key our run added (e.g. antibot clearance) with no current value still merges.
    (tmp_path / BANKED_COOKIES_FILENAME).write_text(
        json.dumps([{"name": "other", "value": "o", "domain": "x.com", "path": "/"}])
    )
    ours = [{"name": "cf_clearance", "value": "CLEAR", "domain": "x.com", "path": "/"}]
    union_cookies_into_profile_dir(ours, str(tmp_path), base_values=seed_cookie_values([]))
    vals = _sidecar_values(tmp_path)
    assert vals[("x.com", "cf_clearance", "/")] == "CLEAR"  # new key applied
    assert vals[("x.com", "other", "/")] == "o"  # unrelated existing key untouched


def test_three_way_applies_when_current_still_equals_seed(tmp_path: Path) -> None:
    # base == theirs: nobody else wrote K since we seeded, so our own change applies.
    (tmp_path / BANKED_COOKIES_FILENAME).write_text(
        json.dumps([{"name": "sid", "value": "SEED", "domain": "x.com", "path": "/"}])
    )
    seed = [{"name": "sid", "value": "SEED", "domain": "x.com", "path": "/"}]
    ours = [{"name": "sid", "value": "OURS", "domain": "x.com", "path": "/"}]
    union_cookies_into_profile_dir(ours, str(tmp_path), base_values=seed_cookie_values(seed))
    assert _sidecar_values(tmp_path)[("x.com", "sid", "/")] == "OURS"  # base==theirs -> ours applied
