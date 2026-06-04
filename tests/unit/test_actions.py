from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from skyvern.webeye.actions.actions import (
    Action,
    ClickAction,
    KeypressAction,
    NullAction,
    SelectOptionAction,
    WebAction,
)
from skyvern.webeye.actions.parse_actions import parse_action


def _mock_scraped_page() -> MagicMock:
    page = MagicMock()
    page.id_to_element_hash = {}
    page.id_to_element_dict = {}
    return page


def test_action_parse__no_element_id() -> None:
    action_no_element_id = {
        "action_type": "click",
    }
    action = Action.model_validate(action_no_element_id)
    assert action.action_type == "click"
    assert action.element_id is None


def test_action_parse__with_element_id() -> None:
    action_no_element_id_str = {
        "action_type": "click",
        "element_id": "element_id",
    }
    action = Action.model_validate(action_no_element_id_str)
    assert action.action_type == "click"
    assert action.element_id == "element_id"

    action_no_element_id_int = {
        "action_type": "click",
        "element_id": 1,
    }
    action = Action.model_validate(action_no_element_id_int)
    assert action.action_type == "click"
    assert action.element_id == "1"


def test_web_action_parse__no_element_id() -> None:
    action_no_element_id = {
        "action_type": "click",
    }
    with pytest.raises(ValidationError):
        WebAction.model_validate(action_no_element_id)


def test_web_action_parse__with_element_id() -> None:
    action_no_element_id_str = {
        "action_type": "click",
        "element_id": "element_id",
    }
    action = WebAction.model_validate(action_no_element_id_str)
    assert action.action_type == "click"
    assert action.element_id == "element_id"

    action_no_element_id_int = {
        "action_type": "click",
        "element_id": 1,
    }
    action = WebAction.model_validate(action_no_element_id_int)
    assert action.action_type == "click"
    assert action.element_id == "1"


@pytest.mark.parametrize("key", ["Enter", "Tab", "Escape", "ArrowDown", "ArrowUp"])
def test_parse_keypress_valid_keys(key: str) -> None:
    action = parse_action(
        action={"action_type": "KEYPRESS", "key": key, "reasoning": "test"},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, KeypressAction)
    assert action.keys == [key]
    assert action.element_id is None
    assert action.skyvern_element_hash is None
    assert action.skyvern_element_data is None


def test_parse_keypress_invalid_key_returns_null_action() -> None:
    action = parse_action(
        action={"action_type": "KEYPRESS", "key": "Delete", "reasoning": "test"},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, NullAction)


def test_parse_keypress_backward_compat_press_enter() -> None:
    action = parse_action(
        action={"action_type": "PRESS_ENTER", "key": "Enter", "reasoning": "test"},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, KeypressAction)
    assert action.keys == ["Enter"]


def test_parse_keypress_keys_list() -> None:
    action = parse_action(
        action={"action_type": "KEYPRESS", "keys": ["Enter"], "reasoning": "test"},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, KeypressAction)
    assert action.keys == ["Enter"]


def test_parse_keypress_no_key_defaults_to_enter() -> None:
    action = parse_action(
        action={"action_type": "KEYPRESS", "reasoning": "test"},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, KeypressAction)
    assert action.keys == ["Enter"]


def test_parse_keypress_repeat_field() -> None:
    action = parse_action(
        action={"action_type": "KEYPRESS", "key": "ArrowDown", "repeat": 3, "reasoning": "test"},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, KeypressAction)
    assert action.keys == ["ArrowDown"]
    assert action.repeat == 3


def test_parse_keypress_repeat_defaults_to_one() -> None:
    action = parse_action(
        action={"action_type": "KEYPRESS", "key": "Enter", "reasoning": "test"},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, KeypressAction)
    assert action.repeat == 1


def test_parse_keypress_repeat_clamped_to_minimum_one() -> None:
    action = parse_action(
        action={"action_type": "KEYPRESS", "key": "Enter", "repeat": 0, "reasoning": "test"},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, KeypressAction)
    assert action.repeat == 1


def test_parse_click_double_click_true() -> None:
    action = parse_action(
        action={"action_type": "CLICK", "id": "1", "reasoning": "test", "double_click": True},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, ClickAction)
    assert action.repeat == 2


def test_parse_click_double_click_false() -> None:
    action = parse_action(
        action={"action_type": "CLICK", "id": "1", "reasoning": "test", "double_click": False},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, ClickAction)
    assert action.repeat == 1


def test_parse_click_no_double_click_field() -> None:
    action = parse_action(
        action={"action_type": "CLICK", "id": "1", "reasoning": "test"},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, ClickAction)
    assert action.repeat == 1


@pytest.mark.parametrize("download_value", [None, False, True])
def test_parse_select_option_download_field(download_value: bool | None) -> None:
    """SELECT_OPTION must parse successfully even when LLM returns download: null (SKY-10453)."""
    action = parse_action(
        action={
            "action_type": "SELECT_OPTION",
            "id": "1",
            "reasoning": "test",
            "download": download_value,
            "option": {"label": "Yes", "index": 1, "value": "Yes"},
        },
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, SelectOptionAction)
    expected = download_value if download_value is not None else False
    assert action.download is expected


def test_parse_select_option_download_missing() -> None:
    """SELECT_OPTION with no download key should default to False."""
    action = parse_action(
        action={
            "action_type": "SELECT_OPTION",
            "id": "1",
            "reasoning": "test",
            "option": {"label": "No", "index": 2, "value": "No"},
        },
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, SelectOptionAction)
    assert action.download is False


@pytest.mark.parametrize("download_value", [None, False, True])
def test_parse_click_download_field(download_value: bool | None) -> None:
    """CLICK must parse successfully even when LLM returns download: null (SKY-10453)."""
    action = parse_action(
        action={
            "action_type": "CLICK",
            "id": "1",
            "reasoning": "test",
            "download": download_value,
        },
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, ClickAction)
    expected = download_value if download_value is not None else False
    assert action.download is expected
