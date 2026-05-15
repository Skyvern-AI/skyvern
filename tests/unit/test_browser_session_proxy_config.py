from pydantic import ValidationError

from skyvern.config import settings
from skyvern.schemas.browser_sessions import CreateBrowserSessionRequest
from skyvern.schemas.proxy_config import BrowserSessionProxyConfig
from skyvern.webeye.browser_factory import BrowserContextFactory


def test_browser_session_proxy_config_redacts_password_in_repr() -> None:
    request = CreateBrowserSessionRequest(
        proxy_config={
            "server": "http://proxy.example.com:8080",
            "username": "proxy-user",
            "password": "proxy-password",
        }
    )

    assert request.proxy_config is not None
    assert request.proxy_config.to_playwright_proxy() == {
        "server": "http://proxy.example.com:8080",
        "username": "proxy-user",
        "password": "proxy-password",
    }
    assert "proxy-password" not in repr(request)


def test_browser_session_proxy_config_requires_server() -> None:
    try:
        CreateBrowserSessionRequest(proxy_config={"username": "proxy-user"})
    except ValidationError as exc:
        assert "server" in str(exc)
    else:
        raise AssertionError("proxy_config without server should fail validation")


def test_browser_args_prefers_request_proxy_config_over_global_pool(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ENABLE_PROXY", True)
    monkeypatch.setattr(settings, "HOSTED_PROXY_POOL", "http://global.example.com:8080")

    proxy_config = BrowserSessionProxyConfig(
        server="socks5://session.example.com:1080",
        username="session-user",
        password="session-password",
        bypass="localhost,127.0.0.1",
    )

    args = BrowserContextFactory.build_browser_args(proxy_config=proxy_config)

    assert args["proxy"] == {
        "server": "socks5://session.example.com:1080",
        "username": "session-user",
        "password": "session-password",
        "bypass": "localhost,127.0.0.1",
    }
