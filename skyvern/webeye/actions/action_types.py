from enum import StrEnum


class ActionType(StrEnum):
    CLICK = "click"
    INPUT_TEXT = "input_text"
    UPLOAD_FILE = "upload_file"

    # This action is not used in the current implementation. Click actions are used instead."
    DOWNLOAD_FILE = "download_file"

    SELECT_OPTION = "select_option"
    CHECKBOX = "checkbox"
    WAIT = "wait"
    NULL_ACTION = "null_action"
    SOLVE_CAPTCHA = "solve_captcha"
    TERMINATE = "terminate"
    COMPLETE = "complete"
    RELOAD_PAGE = "reload_page"

    EXTRACT = "extract"
    VERIFICATION_CODE = "verification_code"

    SCROLL = "scroll"
    KEYPRESS = "keypress"
    TYPE = "type"
    MOVE = "move"
    DRAG = "drag"
    LEFT_MOUSE = "left_mouse"

    def is_web_action(self) -> bool:
        return self in [
            ActionType.CLICK,
            ActionType.INPUT_TEXT,
            ActionType.UPLOAD_FILE,
            ActionType.DOWNLOAD_FILE,
            ActionType.SELECT_OPTION,
            ActionType.CHECKBOX,
        ]
