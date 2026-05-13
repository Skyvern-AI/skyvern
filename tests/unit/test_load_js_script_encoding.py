"""Regression tests for the UTF-8 encoding fix on text-mode file reads (#2116).

Skyvern's ``load_js_script`` (and several CLI / browser-setup callers) previously
used bare ``open(path)`` in text mode. Python's default text-mode codec comes
from ``locale.getpreferredencoding(False)``, which is ``cp936``/``gbk`` on
Chinese Windows locales — causing ``UnicodeDecodeError`` at import time because
``domUtils.js`` ships with non-ASCII characters. These tests pin the ``utf-8``
contract so it cannot regress silently.
"""

from __future__ import annotations

import builtins
from pathlib import Path


def test_dom_utils_js_contains_non_ascii_bytes() -> None:
    """If ``domUtils.js`` ever drops all non-ASCII bytes, the encoding fix
    becomes effectively untested — keep this assertion as a tripwire."""
    from skyvern.constants import SKYVERN_DIR

    path = Path(SKYVERN_DIR) / "webeye" / "scraper" / "domUtils.js"
    data = path.read_bytes()
    assert any(b >= 0x80 for b in data), (
        "domUtils.js no longer contains non-ASCII bytes; the regression test "
        "for #2116 (UnicodeDecodeError on CJK Windows locales) is no longer "
        "meaningful. Either add a non-ASCII character back to domUtils.js or "
        "replace this tripwire with a platform-level locale simulation."
    )


def test_load_js_script_opens_with_utf8(monkeypatch) -> None:
    """``load_js_script`` must pass ``encoding='utf-8'`` so it works on Windows
    locales whose default codec (e.g. cp936/gbk) cannot decode UTF-8 bytes
    beyond 0x7F. Regression guard for #2116."""
    from skyvern.webeye.scraper import scraper as scraper_mod

    real_open = builtins.open
    calls: list[dict] = []

    def spy_open(file, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(file, (str, Path)) and str(file).endswith("domUtils.js"):
            calls.append({"mode": mode, "encoding": kwargs.get("encoding")})
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy_open)

    result = scraper_mod.load_js_script()

    assert result, "load_js_script returned empty content"
    assert calls, "load_js_script did not open domUtils.js"
    # First call to domUtils.js must use text read-mode + explicit utf-8.
    first = calls[0]
    assert first["mode"] in ("r", "rt"), f"unexpected mode: {first['mode']!r}"
    assert first["encoding"] == "utf-8", (
        f"load_js_script must open domUtils.js with encoding='utf-8', "
        f"got {first['encoding']!r} — this will fail on CJK Windows locales."
    )
