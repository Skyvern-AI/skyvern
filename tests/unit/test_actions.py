from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from skyvern.forge.sdk.db.repositories.workflow_parameters import WorkflowParametersRepository
from skyvern.forge.sdk.db.utils import hydrate_action
from skyvern.forge.sdk.schemas.sdk_actions import InputTextAction as SdkInputTextAction
from skyvern.forge.sdk.schemas.sdk_actions import SdkActionType
from skyvern.schemas.steps import AgentStepOutput
from skyvern.utils.action_redaction import (
    REDACTED_OTP_IDENTIFIER,
    REDACTED_OTP_SECRET,
    REDACTED_OTP_URL,
    REDACTED_OTP_VALUE,
    SDK_INPUT_TEXT_ACTION_TYPE,
    redact_action_for_log,
)
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import (
    Action,
    ClickAction,
    ClosePageAction,
    ExtractAction,
    GotoUrlAction,
    InputTextAction,
    KeypressAction,
    NewTabAction,
    NullAction,
    ReloadPageAction,
    SelectOptionAction,
    SwitchTabAction,
    WebAction,
)
from skyvern.webeye.actions.models import DetailedAgentStepOutput
from skyvern.webeye.actions.parse_actions import parse_action


def _mock_scraped_page() -> MagicMock:
    page = MagicMock()
    page.id_to_element_hash = {}
    page.id_to_element_dict = {}
    return page


def test_sdk_input_text_action_type_constant_matches_sdk_enum() -> None:
    assert SDK_INPUT_TEXT_ACTION_TYPE == SdkActionType.AI_INPUT_TEXT.value


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


def test_sdk_input_text_action_repr_redacts_otp_fields() -> None:
    secret_value = "OTP_SECRET_VALUE_SHOULD_NOT_APPEAR"
    secret_identifier = "OTP_IDENTIFIER_SHOULD_NOT_APPEAR"
    secret_url = "OTP_URL_SHOULD_NOT_APPEAR"

    action = SdkInputTextAction(
        selector="#otp-field",
        value=secret_value,
        intention="Enter one-time code",
        totp_identifier=secret_identifier,
        totp_url=secret_url,
    )

    rendered = repr(action)
    rendered_str = str(action)
    raw_payload = action.model_dump()
    log_payload = redact_action_for_log(action)

    assert secret_value not in rendered
    assert secret_identifier not in rendered
    assert secret_url not in rendered
    assert secret_value not in rendered_str
    assert secret_identifier not in rendered_str
    assert secret_url not in rendered_str
    assert raw_payload["value"] == secret_value
    assert raw_payload["totp_identifier"] == secret_identifier
    assert raw_payload["totp_url"] == secret_url
    assert secret_value not in str(log_payload)
    assert secret_identifier not in str(log_payload)
    assert secret_url not in str(log_payload)
    assert REDACTED_OTP_VALUE in rendered
    assert REDACTED_OTP_VALUE in rendered_str
    assert REDACTED_OTP_IDENTIFIER in rendered_str
    assert REDACTED_OTP_URL in rendered_str
    assert log_payload["value"] == REDACTED_OTP_VALUE
    assert "#otp-field" in rendered
    assert "Enter one-time code" in rendered


def test_web_input_text_action_repr_redacts_otp_text() -> None:
    secret_value = "OTP_SECRET_VALUE_SHOULD_NOT_APPEAR"
    action = InputTextAction(
        action_type=ActionType.INPUT_TEXT,
        element_id="otp-field",
        text=secret_value,
        intention="Enter verification code",
        response=secret_value,
        totp_code_required=True,
    )

    rendered = repr(action)
    rendered_str = str(action)
    raw_payload = action.model_dump()
    log_payload = redact_action_for_log(action)

    assert secret_value not in rendered
    assert secret_value not in rendered_str
    assert raw_payload["text"] == secret_value
    assert raw_payload["response"] == secret_value
    assert secret_value not in str(log_payload)
    assert REDACTED_OTP_VALUE in rendered
    assert REDACTED_OTP_VALUE in rendered_str
    assert log_payload["text"] == REDACTED_OTP_VALUE
    assert log_payload["response"] == REDACTED_OTP_VALUE
    assert "otp-field" in rendered


def test_web_input_text_action_repr_redacts_otp_text_marked_by_identifier() -> None:
    secret_value = "OTP_SECRET_VALUE_SHOULD_NOT_APPEAR"
    secret_identifier = "OTP_IDENTIFIER_SHOULD_NOT_APPEAR"
    action = InputTextAction(
        action_type=ActionType.INPUT_TEXT,
        element_id="otp-field",
        text=secret_value,
        intention="Enter code",
        response=secret_value,
        totp_identifier=secret_identifier,
    )

    rendered = repr(action)
    rendered_str = str(action)
    raw_payload = action.model_dump()
    log_payload = redact_action_for_log(action)

    assert secret_value not in rendered
    assert secret_value not in rendered_str
    assert secret_identifier not in rendered_str
    assert raw_payload["text"] == secret_value
    assert raw_payload["totp_identifier"] == secret_identifier
    assert log_payload["text"] == REDACTED_OTP_VALUE
    assert log_payload["response"] == REDACTED_OTP_VALUE
    assert log_payload["totp_identifier"] == REDACTED_OTP_IDENTIFIER
    assert secret_value not in str(log_payload)
    assert secret_identifier not in str(log_payload)
    assert REDACTED_OTP_VALUE in rendered_str


def test_step_output_serialization_redacts_otp_input_action() -> None:
    secret_value = "OTP_SECRET_VALUE_SHOULD_NOT_APPEAR"
    action = InputTextAction(
        action_type=ActionType.INPUT_TEXT,
        element_id="otp-field",
        text=secret_value,
        intention="Enter verification code",
        response=secret_value,
        totp_code_required=True,
    )

    payload = AgentStepOutput(actions_and_results=[(action, [])]).model_dump()

    assert secret_value not in str(payload)
    assert payload["actions_and_results"][0][0]["text"] == REDACTED_OTP_VALUE
    assert payload["actions_and_results"][0][0]["response"] == REDACTED_OTP_VALUE


def test_detailed_step_output_debug_repr_redacts_otp_input_action(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.config.settings.DEBUG_MODE", True)
    secret_value = "OTP_SECRET_VALUE_SHOULD_NOT_APPEAR"
    secret_identifier = "OTP_IDENTIFIER_SHOULD_NOT_APPEAR"
    secret_url = "OTP_URL_SHOULD_NOT_APPEAR"
    action = InputTextAction(
        action_type=ActionType.INPUT_TEXT,
        element_id="otp-field",
        text=secret_value,
        intention="Enter verification code",
        response=secret_value,
        totp_identifier=secret_identifier,
        totp_url=secret_url,
    )

    rendered = repr(
        DetailedAgentStepOutput(
            scraped_page=None,
            extract_action_prompt=None,
            llm_response=None,
            actions=[action],
            action_results=None,
            actions_and_results=[(action, [])],
        )
    )

    assert secret_value not in rendered
    assert secret_identifier not in rendered
    assert secret_url not in rendered
    assert REDACTED_OTP_VALUE in rendered
    assert REDACTED_OTP_IDENTIFIER in rendered
    assert REDACTED_OTP_URL in rendered


def test_action_log_payload_redacts_otp_text_response_and_timing_secret() -> None:
    secret_value = "OTP_SECRET_VALUE_SHOULD_NOT_APPEAR"
    timing_secret = "OTP_TIMING_SECRET_SHOULD_NOT_APPEAR"
    action = InputTextAction(
        action_type=ActionType.INPUT_TEXT,
        element_id="otp-field",
        text=secret_value,
        intention="Enter passcode",
        response=secret_value,
        totp_timing_info={"is_totp_sequence": True, "totp_secret": timing_secret, "action_index": 0},
    )

    payload = redact_action_for_log(action)

    assert payload["text"] == REDACTED_OTP_VALUE
    assert payload["response"] == REDACTED_OTP_VALUE
    assert payload["totp_timing_info"]["totp_secret"] == REDACTED_OTP_SECRET
    assert secret_value not in str(payload)
    assert timing_secret not in str(payload)
    assert payload["element_id"] == "otp-field"
    assert payload["action_type"] == ActionType.INPUT_TEXT


def test_action_log_payload_keeps_non_otp_input_debuggable() -> None:
    action = InputTextAction(
        action_type=ActionType.INPUT_TEXT,
        element_id="account-field",
        text="SAFE_ACCOUNT_REFERENCE",
        intention="Enter account reference",
        response="SAFE_ACCOUNT_REFERENCE",
    )

    payload = redact_action_for_log(action)

    assert payload["text"] == "SAFE_ACCOUNT_REFERENCE"
    assert payload["response"] == "SAFE_ACCOUNT_REFERENCE"
    assert payload["element_id"] == "account-field"
    assert payload["intention"] == "Enter account reference"


@pytest.mark.asyncio
async def test_create_action_redacts_response_but_preserves_action_json_for_hydration() -> None:
    secret_value = "OTP_SECRET_VALUE_SHOULD_NOT_APPEAR"
    captured_models = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def add(self, model) -> None:
            captured_models.append(model)

        async def commit(self) -> None:
            pass

        async def refresh(self, model) -> None:
            pass

    repo = WorkflowParametersRepository(lambda: FakeSession())
    action = InputTextAction(
        action_type=ActionType.INPUT_TEXT,
        organization_id="o_test",
        workflow_run_id="wr_test",
        task_id="tsk_test",
        step_id="stp_test",
        step_order=0,
        action_order=0,
        element_id="otp-field",
        text=secret_value,
        intention="Enter verification code",
        response=secret_value,
        totp_code_required=True,
    )

    await repo.create_action(action)

    persisted_model = captured_models[0]
    assert persisted_model.response == REDACTED_OTP_VALUE
    assert persisted_model.action_json["text"] == secret_value
    assert persisted_model.action_json["response"] == secret_value

    hydrated_action = hydrate_action(persisted_model)
    assert isinstance(hydrated_action, InputTextAction)
    assert hydrated_action.text == secret_value
    assert hydrated_action.response == secret_value


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


def test_parse_close_page_with_tab_index() -> None:
    action = parse_action(
        action={"action_type": "CLOSE_PAGE", "tab_index": 3, "reasoning": "drop the extra tab"},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, ClosePageAction)
    assert action.tab_index == 3


def test_parse_close_page_without_tab_index_defaults_to_current() -> None:
    action = parse_action(
        action={"action_type": "CLOSE_PAGE", "reasoning": "close current"},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, ClosePageAction)
    assert action.tab_index is None


def test_parse_close_page_non_integer_tab_index_falls_back_to_current() -> None:
    action = parse_action(
        action={"action_type": "CLOSE_PAGE", "tab_index": "not-a-number", "reasoning": "bad index"},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, ClosePageAction)
    assert action.tab_index is None


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


@pytest.mark.parametrize("action_type", ["EXTRACT_INFORMATION", "EXTRACT", "extract_information"])
def test_parse_extract_information_with_extraction_goal(action_type: str) -> None:
    schema = {"type": "object", "properties": {"price": {"type": "string"}}}
    action = parse_action(
        action={"action_type": action_type, "id": None, "reasoning": "test"},
        scraped_page=_mock_scraped_page(),
        data_extraction_goal="extract the price",
        extracted_information_schema=schema,
    )
    assert isinstance(action, ExtractAction)
    assert action.data_extraction_goal == "extract the price"
    assert action.data_extraction_schema == schema
    assert action.element_id is None
    assert action.skyvern_element_hash is None
    assert action.skyvern_element_data is None


def test_parse_extract_information_clears_hallucinated_element_id() -> None:
    action = parse_action(
        action={"action_type": "EXTRACT_INFORMATION", "id": "42", "reasoning": "test"},
        scraped_page=_mock_scraped_page(),
        data_extraction_goal="extract the price",
    )
    assert isinstance(action, ExtractAction)
    assert action.element_id is None


def test_parse_extract_information_without_extraction_goal_returns_null_action() -> None:
    action = parse_action(
        action={"action_type": "EXTRACT_INFORMATION", "id": None, "reasoning": "test"},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, NullAction)


def test_parse_goto_url_valid_url() -> None:
    action = parse_action(
        action={"action_type": "GOTO_URL", "id": None, "url": "https://example.com/a", "reasoning": "test"},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, GotoUrlAction)
    assert action.url == "https://example.com/a"
    assert action.element_id is None
    assert action.skyvern_element_hash is None
    assert action.skyvern_element_data is None
    assert action.is_magic_link is False


def test_parse_goto_url_prepends_https_scheme() -> None:
    action = parse_action(
        action={"action_type": "GOTO_URL", "id": None, "url": "example.com/a", "reasoning": "test"},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, GotoUrlAction)
    assert action.url == "https://example.com/a"


def test_parse_goto_url_clears_hallucinated_element_id() -> None:
    action = parse_action(
        action={"action_type": "GOTO_URL", "id": "7", "url": "https://example.com", "reasoning": "test"},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, GotoUrlAction)
    assert action.element_id is None


@pytest.mark.parametrize("url", [None, "", "ftp://example.com", "not a url"])
def test_parse_goto_url_invalid_or_missing_url_returns_null_action(url: str | None) -> None:
    action = parse_action(
        action={"action_type": "GOTO_URL", "id": None, "url": url, "reasoning": "test"},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, NullAction)


def test_parse_goto_url_without_url_key_returns_null_action() -> None:
    action = parse_action(
        action={"action_type": "GOTO_URL", "id": None, "reasoning": "test"},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, NullAction)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/admin",
        "http://127.0.0.1/latest",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.5/internal",
    ],
)
def test_parse_goto_url_blocked_host_returns_null_action(url: str) -> None:
    action = parse_action(
        action={"action_type": "GOTO_URL", "id": None, "url": url, "reasoning": "test"},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, NullAction)


def test_parse_reload_page() -> None:
    action = parse_action(
        action={"action_type": "RELOAD_PAGE", "id": None, "reasoning": "test"},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, ReloadPageAction)
    assert action.element_id is None


def test_parse_new_tab_action_with_url() -> None:
    action = parse_action(
        action={"action_type": "NEW_TAB", "url": "https://example.test/page", "reasoning": "open a separate tab"},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, NewTabAction)
    assert action.url == "https://example.test/page"
    assert action.element_id is None


def test_parse_new_tab_action_prepends_scheme() -> None:
    action = parse_action(
        action={"action_type": "NEW_TAB", "url": "example.test/page", "reasoning": "test"},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, NewTabAction)
    assert action.url == "https://example.test/page"


def test_parse_new_tab_action_missing_url_returns_null() -> None:
    action = parse_action(
        action={"action_type": "NEW_TAB", "reasoning": "test"},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, NullAction)


def test_parse_new_tab_action_blocked_host_returns_null() -> None:
    action = parse_action(
        action={"action_type": "NEW_TAB", "url": "http://localhost:8000/admin", "reasoning": "test"},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, NullAction)


def test_parse_switch_tab_action_valid_index() -> None:
    action = parse_action(
        action={"action_type": "SWITCH_TAB", "tab_index": 1, "reasoning": "go back to first tab"},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, SwitchTabAction)
    assert action.tab_index == 1
    assert action.element_id is None


def test_parse_switch_tab_action_coerces_string_index() -> None:
    action = parse_action(
        action={"action_type": "SWITCH_TAB", "tab_index": "2", "reasoning": "test"},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, SwitchTabAction)
    assert action.tab_index == 2


def test_parse_switch_tab_action_missing_index_returns_null() -> None:
    action = parse_action(
        action={"action_type": "SWITCH_TAB", "reasoning": "test"},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, NullAction)


def test_parse_switch_tab_action_non_integer_index_returns_null() -> None:
    action = parse_action(
        action={"action_type": "SWITCH_TAB", "tab_index": "not-a-number", "reasoning": "test"},
        scraped_page=_mock_scraped_page(),
    )
    assert isinstance(action, NullAction)


def test_tab_actions_registered_for_db_hydration() -> None:
    from skyvern.forge.sdk.db.utils import ACTION_TYPE_TO_CLASS
    from skyvern.webeye.actions.action_types import ActionType

    assert ACTION_TYPE_TO_CLASS[ActionType.NEW_TAB] is NewTabAction
    assert ACTION_TYPE_TO_CLASS[ActionType.SWITCH_TAB] is SwitchTabAction
