import sys
import types
from importlib import import_module
from pathlib import Path

import pytest

pytest.skip("Dependencies missing", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
ddtrace_stub = types.SimpleNamespace(tracer=None, filters=types.SimpleNamespace(FilterRequestsOnUrl=lambda x: None))
sys.modules.setdefault("ddtrace", ddtrace_stub)
sys.modules.setdefault("ddtrace.filters", ddtrace_stub.filters)

encode_url = import_module("skyvern.utils.url_validators").encode_url


def test_encode_url_basic():
    """Test basic URL encoding with simple path"""
    url = "https://example.com/path with spaces"
    expected = "https://example.com/path%20with%20spaces"
    assert encode_url(url) == expected


def test_encode_url_with_query_params():
    """Test URL encoding with query parameters"""
    url = "https://example.com/search?q=hello world&type=test"
    expected = "https://example.com/search?q=hello%20world&type=test"
    assert encode_url(url) == expected


def test_encode_url_with_special_chars():
    """Test URL encoding with special characters"""
    url = "https://example.com/path/with/special#chars?param=value&other=test@123"
    expected = "https://example.com/path/with/special#chars?param=value&other=test@123"
    assert encode_url(url) == expected


def test_encode_url_with_pre_encoded_chars():
    """Test URL encoding with pre-encoded characters in query parameters"""
    url = "https://example.com/search?q=hello world&type=test%20test"
    expected = "https://example.com/search?q=hello%20world&type=test%20test"
    assert encode_url(url) == expected
