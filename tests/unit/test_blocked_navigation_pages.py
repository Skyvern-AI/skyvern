from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from skyvern.webeye.real_browser_state import RealBrowserState


def _page(url: str) -> MagicMock:
    page = MagicMock()
    page.url = url
    page.is_closed.return_value = False
    return page


async def test_list_valid_pages_hides_a_page_that_navigated_itself_to_an_internal_host(
    monkeypatch,
) -> None:
    def fail_public_dns(host: str, *args: object, **kwargs: object) -> list[object]:
        raise OSError("dns unavailable")

    monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", fail_public_dns)
    public_page = _page("https://public.example.test/page")
    blank_page = _page("about:blank")
    internal_page = _page("http://127.0.0.1:8080/admin")
    browser_context = SimpleNamespace(pages=[public_page, blank_page, internal_page])
    state = RealBrowserState(pw=MagicMock(), browser_context=browser_context)

    pages = await state.list_valid_pages(max_pages=0)

    assert pages == [public_page, blank_page]
