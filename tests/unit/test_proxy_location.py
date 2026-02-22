import pytest

from skyvern.forge.sdk.db.utils import _deserialize_proxy_location
from skyvern.forge.sdk.schemas.task_v2 import TaskV2
from skyvern.schemas.runs import ProxyLocation
from skyvern.webeye.browser_factory import BrowserContextFactory

def test_deserialize_proxy_location():
    assert _deserialize_proxy_location("RESIDENTIAL") == ProxyLocation.RESIDENTIAL
    assert _deserialize_proxy_location("http://user:pass@127.0.0.1:8080") == "http://user:pass@127.0.0.1:8080"
    assert _deserialize_proxy_location("socks5://10.0.0.1:1080") == "socks5://10.0.0.1:1080"
    assert _deserialize_proxy_location(None) is None

def test_parse_proxy_location():
    assert TaskV2._parse_proxy_location("RESIDENTIAL") == ProxyLocation.RESIDENTIAL
    assert TaskV2._parse_proxy_location("http://user:pass@127.0.0.1:8080") == "http://user:pass@127.0.0.1:8080"
    assert TaskV2._parse_proxy_location("socks5://10.0.0.1:1080") == "socks5://10.0.0.1:1080"
    assert TaskV2._parse_proxy_location(None) is None

def test_build_browser_args_with_custom_proxy():
    # Test valid custom proxy url
    proxy_url = "http://username:password@myproxy.com:8080"
    args = BrowserContextFactory.build_browser_args(proxy_location=proxy_url)
    assert args.get("proxy") == {
        "server": "http://myproxy.com:8080",
        "username": "username",
        "password": "password",
    }
    assert args.get("timezone_id") is None

    # Test invalid custom proxy url (should fall back to no custom proxy)
    invalid_proxy = "not a proxy url"
    args = BrowserContextFactory.build_browser_args(proxy_location=invalid_proxy)
    # The proxy key might not exist if ENABLE_PROXY is False in tests, 
    # but at least custom proxy should not be parsed
    if "proxy" in args:
        assert args["proxy"].get("server") != "not a proxy url"
