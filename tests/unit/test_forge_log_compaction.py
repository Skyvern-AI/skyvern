from skyvern.forge.sdk.forge_log import (
    compact_action_objects,
)


class _FakeAction:
    def __init__(self, action_id: str = "act_1", action_type: str = "click", element_id: str = "AAA") -> None:
        self.action_id = action_id
        self.action_type = action_type
        self.element_id = element_id
        self.reasoning = "x" * 5000
        self.intention = "y" * 5000
        self.response = "z" * 5000


class _FakeResult:
    def __init__(self, success: bool) -> None:
        self.success = success


def test_compact_action_replaces_object_with_three_keys() -> None:
    event = {"event": "Handling action", "action": _FakeAction()}
    result = compact_action_objects(None, "info", event)  # type: ignore[arg-type]
    assert result["action"] == {"id": "act_1", "type": "click", "element_id": "AAA"}


def test_compact_action_leaves_primitive_untouched() -> None:
    event = {"event": "msg", "action": "literal-string"}
    result = compact_action_objects(None, "info", event)  # type: ignore[arg-type]
    assert result["action"] == "literal-string"


def test_compact_action_handles_missing_attrs_defensively() -> None:
    class _Bare:
        pass

    event = {"event": "msg", "action": _Bare()}
    result = compact_action_objects(None, "info", event)  # type: ignore[arg-type]
    assert result["action"] == {"id": None, "type": "_Bare", "element_id": None}


def test_compact_action_result_summarizes_list() -> None:
    event = {"event": "Action succeeded", "action_result": [_FakeResult(True), _FakeResult(True)]}
    result = compact_action_objects(None, "info", event)  # type: ignore[arg-type]
    assert result["action_result"] == {"count": 2, "success": True}


def test_compact_action_result_flags_partial_failure() -> None:
    event = {"event": "Action failed", "action_result": [_FakeResult(True), _FakeResult(False)]}
    result = compact_action_objects(None, "info", event)  # type: ignore[arg-type]
    assert result["action_result"] == {"count": 2, "success": False}


def test_compact_action_passthrough_when_keys_absent() -> None:
    event = {"event": "msg", "step_order": 0}
    result = compact_action_objects(None, "info", event)  # type: ignore[arg-type]
    assert result == event


def test_compact_plural_actions_list() -> None:
    event = {"event": "Executing actions", "actions": [_FakeAction("act_1"), _FakeAction("act_2")]}
    result = compact_action_objects(None, "info", event)  # type: ignore[arg-type]
    assert result["actions"] == [
        {"id": "act_1", "type": "click", "element_id": "AAA"},
        {"id": "act_2", "type": "click", "element_id": "AAA"},
    ]


def test_compact_plural_actions_leaves_an_empty_list_alone() -> None:
    event = {"event": "msg", "actions": []}
    assert compact_action_objects(None, "info", event)["actions"] == []  # type: ignore[arg-type]


def test_plural_actions_compaction_is_not_a_redaction_control() -> None:
    """It keeps a signed file_url out of THIS shape as a side effect, and that is all it does.

    Nothing here inspects a value or a nested container: `cached_action=`, `next_action=` and a bare
    `download_url=` string all pass through untouched, exactly as on main. Credential redaction
    across the logging stack is a separate concern with its own ticket, and treating this as one
    would be the mistake the docstring warns about.
    """
    from skyvern.webeye.actions.actions import ClickAction

    signed = "https://example.com/a.pdf?X-Amz-Signature=SIGSECRET0123456789"
    event = {"event": "line", "actions": [ClickAction(element_id="1", file_url=signed)], "cached_action": signed}
    result = compact_action_objects(None, "info", event)  # type: ignore[arg-type]

    assert result["actions"] == [{"id": None, "type": "click", "element_id": "1"}]
    assert result["cached_action"] == signed
