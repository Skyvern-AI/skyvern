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
