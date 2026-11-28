"""A persistent context (no owning Browser) still uses the bounded HTML path."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).parents[2] / "skyvern/webeye/utils/_html_content.py"


def _load_module(monkeypatch):
    def stub(name, **attrs):
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    stub("playwright")
    stub("playwright.async_api", CDPSession=object, Frame=object, Page=object)

    class AgentFunctions:
        async def html_content_bounded_transport_enabled(self):
            return True

    forge = stub("skyvern.forge")
    forge.app = types.SimpleNamespace(AGENT_FUNCTION=AgentFunctions())
    for name in ("skyvern", "skyvern.webeye", "skyvern.webeye.utils"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    sys.modules["skyvern"].__path__ = [str(_MODULE_PATH.parents[2])]
    sys.modules["skyvern.webeye"].__path__ = [str(_MODULE_PATH.parents[1])]
    sys.modules["skyvern.webeye.utils"].__path__ = [str(_MODULE_PATH.parent)]

    stub(
        "skyvern.webeye.action_deadline",
        cancellable_driver_call=lambda call: call(),
        raise_if_cancelled=lambda: None,
    )
    stub("skyvern.webeye.browser_engine", BrowserEngineSelection=object)
    stub("skyvern.webeye.browser_object_predicates", is_page_like=lambda obj: True)

    spec = importlib.util.spec_from_file_location("_html_content_under_test", _MODULE_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_read_html__persistent_context__uses_bounded_transport(monkeypatch):
    module = _load_module(monkeypatch)

    class Session:
        HTML = "<html><body>bounded</body></html>"

        def __init__(self):
            self.sent = []

        async def send(self, method, params=None):
            self.sent.append(method)
            if method == "Page.getFrameTree":
                return {"frameTree": {"frame": {"id": "f1"}}}
            if method == "Page.createIsolatedWorld":
                return {"executionContextId": 1}
            if method == "Runtime.evaluate":
                return {"result": {"objectId": "o1"}}
            if method == "Runtime.callFunctionOn":
                declaration = (params or {}).get("functionDeclaration", "")
                if "html.length" in declaration:
                    return {"result": {"value": len(self.HTML)}}
                if "this.html" in declaration:
                    return {"result": {"value": self.HTML}}
            return {}

        async def detach(self):
            return None

    session = Session()

    class Context:
        browser = None

        async def new_cdp_session(self, page):
            return session

    class Page:
        def __init__(self):
            self.context = Context()
            self.content_calls = 0

        async def content(self):
            self.content_calls += 1
            return "<html>plain</html>"

    page = Page()
    selection = types.SimpleNamespace(metadata=types.SimpleNamespace(supports_bounded_html=True))

    result = await module.read_html(page, selection)

    assert "<body>bounded</body>" in result
    assert session.sent
    assert page.content_calls == 0
