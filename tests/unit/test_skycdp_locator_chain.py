import pytest

from skyvern.webeye.skycdp.errors import CdpError
from skyvern.webeye.skycdp.facade.locator import _query_operations, _split_selector_chain


def test_nth_selector_step_becomes_an_index_operation() -> None:
    kinds = [(op.kind, op.selector, op.index) for op in _query_operations("ul#list >> nth=-1 >> li")]
    assert kinds == [("query", "ul#list", None), ("nth", None, -1), ("query", "li", None)]


def test_malformed_nth_selector_step_is_a_cdp_error() -> None:
    with pytest.raises(CdpError, match="nth=abc"):
        _query_operations("div >> nth=abc")


def test_apostrophe_in_an_unquoted_text_body_does_not_swallow_the_next_step() -> None:
    assert _split_selector_chain("text=Men's >> nth=0") == ["text=Men's", "nth=0"]
    assert _split_selector_chain("text='Men's shoes' >> nth=0") == ["text='Men's shoes'", "nth=0"]
    assert _split_selector_chain('li:has-text("a >> b") >> nth=0') == ['li:has-text("a >> b")', "nth=0"]


def test_an_escaped_quote_does_not_swallow_the_next_step() -> None:
    assert _split_selector_chain(r"div[title='John\'s'] >> nth=0") == [r"div[title='John\'s']", "nth=0"]
    assert _split_selector_chain(r'div[title="a\"b"] >> nth=0') == [r'div[title="a\"b"]', "nth=0"]
    assert _split_selector_chain(r"li:has-text('a >> b') >> nth=0") == [r"li:has-text('a >> b')", "nth=0"]
