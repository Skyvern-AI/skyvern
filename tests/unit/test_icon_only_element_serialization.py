"""
Tests for icon-only interactable element serialization.

Icon-only navigation controls (e.g. CSS-rendered arrow buttons with no visible text)
need their class attribute and pseudo-text preserved through the trim/serialization
pipeline so LLMs can identify them.
"""

import copy

import pytest

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.webeye.scraper.scraped_page import _replace_pua_with_marker, json_to_html
from skyvern.webeye.scraper.scraper import _trimmed_attributes, trim_element


@pytest.fixture(autouse=True)
def _skyvern_ctx():
    with skyvern_context.scoped(SkyvernContext(organization_id="o_test")):
        yield


ICON_ONLY_NAV_ELEMENT = {
    "id": "AACO",
    "frame": "main.frame",
    "frame_index": 0,
    "interactable": True,
    "hoverOnly": False,
    "tagName": "div",
    "attributes": {
        "class": "icon-nav forward",
        "id": "widget_10",
        "lang": "en",
        "unique_id": "AACO",
    },
    "beforePseudoText": None,
    "text": "",
    "afterPseudoText": "\uf061",
    "children": [],
    "purgeable": False,
    "keepAllAttr": False,
    "isSelectable": False,
}

NORMAL_TEXT_BUTTON = {
    "id": "NEXT1",
    "frame": "main.frame",
    "frame_index": 0,
    "interactable": True,
    "tagName": "button",
    "attributes": {
        "class": "btn-primary large-button extra-styles",
        "type": "submit",
        "unique_id": "NEXT1",
    },
    "text": "Submit",
    "beforePseudoText": None,
    "afterPseudoText": None,
    "children": [],
    "purgeable": False,
    "keepAllAttr": False,
    "isSelectable": False,
}

NON_INTERACTABLE_DIV = {
    "id": "DIV1",
    "frame": "main.frame",
    "frame_index": 0,
    "interactable": False,
    "tagName": "div",
    "attributes": {
        "class": "layout-wrapper",
        "unique_id": "DIV1",
    },
    "text": "",
    "beforePseudoText": None,
    "afterPseudoText": "\uf061",
    "children": [],
    "purgeable": False,
    "keepAllAttr": False,
    "isSelectable": False,
}


class TestTrimmedAttributesClassPreservation:
    def test_icon_only_interactable_preserves_class(self):
        attrs = {"class": "icon-nav forward", "data-dojo-type": "dojosite/Noop", "lang": "en"}
        result = _trimmed_attributes(attrs, keep_class=True)
        assert result["class"] == "icon-nav forward"

    def test_normal_element_strips_class(self):
        attrs = {"class": "btn-primary", "type": "submit"}
        result = _trimmed_attributes(attrs, keep_class=False)
        assert "class" not in result
        assert result["type"] == "submit"

    def test_long_class_truncated(self):
        long_class = "tw-" + "x" * 200
        attrs = {"class": long_class}
        result = _trimmed_attributes(attrs, keep_class=True)
        assert len(result["class"]) == 100

    def test_reserved_attrs_always_kept(self):
        attrs = {"aria-label": "Next", "class": "appGo", "lang": "en"}
        result = _trimmed_attributes(attrs, keep_class=False)
        assert result["aria-label"] == "Next"
        assert "class" not in result
        assert "lang" not in result


class TestTrimElementIconOnly:
    def test_icon_nav_element_retains_class_after_trim(self):
        el = copy.deepcopy(ICON_ONLY_NAV_ELEMENT)
        trimmed = trim_element(el)
        assert "attributes" in trimmed
        assert trimmed["attributes"].get("class") == "icon-nav forward"

    def test_text_button_does_not_retain_class(self):
        el = copy.deepcopy(NORMAL_TEXT_BUTTON)
        trimmed = trim_element(el)
        attrs = trimmed.get("attributes", {})
        assert "class" not in attrs
        assert attrs.get("type") == "submit"

    def test_non_interactable_icon_does_not_retain_class(self):
        el = copy.deepcopy(NON_INTERACTABLE_DIV)
        trimmed = trim_element(el)
        attrs = trimmed.get("attributes", {})
        assert "class" not in attrs


class TestPUAReplacement:
    def test_fontawesome_arrow_right(self):
        assert _replace_pua_with_marker("\uf061") == "[icon]"

    def test_multiple_pua_chars(self):
        assert _replace_pua_with_marker("\uf061\uf062") == "[icon]"

    def test_mixed_pua_and_text(self):
        assert _replace_pua_with_marker("Next \uf061") == "Next [icon]"

    def test_no_pua(self):
        assert _replace_pua_with_marker("Next") == "Next"

    def test_empty_string(self):
        assert _replace_pua_with_marker("") == ""

    def test_none_returns_empty(self):
        assert _replace_pua_with_marker(None) == ""


class TestJsonToHtmlWithIconElements:
    def test_icon_nav_after_trim_produces_readable_output(self):
        el = copy.deepcopy(ICON_ONLY_NAV_ELEMENT)
        trimmed = trim_element(el)
        html = json_to_html(trimmed)
        assert 'class="icon-nav forward"' in html
        assert "[icon]" in html
        assert "AACO" in html

    def test_text_button_no_icon_marker(self):
        el = copy.deepcopy(NORMAL_TEXT_BUTTON)
        trimmed = trim_element(el)
        html = json_to_html(trimmed)
        assert "Submit" in html
        assert "[icon]" not in html
        assert "class=" not in html

    def test_before_pseudo_pua_also_replaced(self):
        el = copy.deepcopy(ICON_ONLY_NAV_ELEMENT)
        el["beforePseudoText"] = "\uf060"
        el["afterPseudoText"] = None
        trimmed = trim_element(el)
        html = json_to_html(trimmed)
        assert "[icon]" in html
