from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from skyvern.webeye.actions.actions import Action, KeypressAction, NullAction, WebAction
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
