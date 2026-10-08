"""Direct unit tests for ``_ui_select_commit_result`` — the ui-select commit verification predicate.

The owner-key gate (SKY-6657) confirms a commit whose visible closed name is shared across keys by the
strictly owner-scoped, type-tagged ngModel key, while the receipt still records the observed display text.
``ownerKey`` values here use the handler's ``"<type>:<value>"`` shape; synthetic DUMMY keys only.
"""

from __future__ import annotations

import pytest

from skyvern.webeye.actions.actions import InputTextAction
from skyvern.webeye.actions.handler import _ui_select_commit_result
from skyvern.webeye.actions.responses import ActionFailure, ActionSuccess


def _action(stop: bool = False) -> InputTextAction:
    action = InputTextAction(element_id="probe-el", text="probe", reasoning="probe")
    action.stop_batch_after_dropdown_select = stop
    return action


def test_samename_distinct_owner_key_commits_and_records_display_text() -> None:
    """The load-bearing case: preselected X, a legitimately-clicked same-name row with a DISTINCT owner key.
    The text arms cannot confirm (shared name), the owner-key gate fires, and the receipt records the
    observed DISPLAY text (not the key)."""
    pre = {
        "choicesOpen": True,
        "searchValue": "North Site (B)",
        "matchTexts": [],
        "latentMatchTexts": ["North Site"],
        "ownerKey": "string:DUMMY-1001",
    }
    post = {
        "choicesOpen": False,
        "searchValue": "",
        "matchTexts": ["North Site"],
        "latentMatchTexts": ["North Site"],
        "ownerKey": "string:DUMMY-1002",
    }
    result = _ui_select_commit_result(_action(stop=True), pre, post, "North Site (B)", "North Site (B)")
    assert isinstance(result, ActionSuccess)
    assert result.committed_value == "North Site"  # observed display text, never the raw key
    assert result.committed_option == "North Site (B)"
    assert result.skip_remaining_actions is True


def test_ordinary_unique_commits_via_text_arm_records_display_text() -> None:
    """An ordinary unique selection is confirmed by the genuinely-new display-text arm before the gate is
    reached, so the receipt is the display text and the fix does not regress the text-only path."""
    pre = {"choicesOpen": True, "searchValue": "South Site", "matchTexts": [], "latentMatchTexts": ["Select account"]}
    post = {
        "choicesOpen": False,
        "searchValue": "",
        "matchTexts": ["Select account South Site"],
        "ownerKey": "string:DUMMY-2002",
    }
    result = _ui_select_commit_result(_action(), pre, post, "South Site", "South Site")
    assert isinstance(result, ActionSuccess)
    assert result.committed_value == "Select account South Site"


def test_type_tagged_owner_key_distinguishes_numeric_from_string() -> None:
    """Codex P2: two same-label options whose keys differ only by type (numeric 1 vs string "1") must be seen
    as a genuine change. Type-tagging keeps them distinct so the gate fires."""
    pre = {
        "choicesOpen": True,
        "searchValue": "Option",
        "matchTexts": [],
        "latentMatchTexts": ["Option"],
        "ownerKey": "number:1",
    }
    post = {
        "choicesOpen": False,
        "searchValue": "",
        "matchTexts": ["Option"],
        "latentMatchTexts": ["Option"],
        "ownerKey": "string:1",
    }
    result = _ui_select_commit_result(_action(), pre, post, "Option", "Option")
    assert isinstance(result, ActionSuccess)
    assert result.committed_value == "Option"


@pytest.mark.parametrize(
    ("pre_key", "post_key"),
    [
        pytest.param(None, None, id="unreadable-both"),
        pytest.param("string:DUMMY-1001", "string:DUMMY-1001", id="unchanged-key"),
        pytest.param("string:DUMMY-1001", None, id="key-then-null"),
    ],
)
def test_owner_key_fails_closed(pre_key: str | None, post_key: str | None) -> None:
    """Shared visible name, candidate label distinct from the closed name, and no USABLE key change
    (unreadable, unchanged, or a key that becomes null after the click) -> fail closed. The key-then-null
    case pins the ``is not None`` guard: dropping it would wrongly accept with committed_value="None"."""
    pre = {
        "choicesOpen": True,
        "searchValue": "North Site (B)",
        "matchTexts": [],
        "latentMatchTexts": ["North Site"],
        "ownerKey": pre_key,
    }
    post = {
        "choicesOpen": False,
        "searchValue": "",
        "matchTexts": ["North Site"],
        "latentMatchTexts": ["North Site"],
        "ownerKey": post_key,
    }
    result = _ui_select_commit_result(_action(), pre, post, "North Site (B)", "North Site (B)")
    assert isinstance(result, ActionFailure)


def test_clean_no_op_returns_none() -> None:
    """Byte-identical clean no-op (choices still open, search text unchanged, identical match texts) -> None,
    even with an unchanged owner key present (the fall-through contract must be preserved)."""
    pre = {
        "choicesOpen": True,
        "searchValue": "North",
        "matchTexts": ["North Site"],
        "latentMatchTexts": ["North Site"],
        "ownerKey": "string:DUMMY-1001",
    }
    post = {
        "choicesOpen": True,
        "searchValue": "North",
        "matchTexts": ["North Site"],
        "latentMatchTexts": ["North Site"],
        "ownerKey": "string:DUMMY-1001",
    }
    result = _ui_select_commit_result(_action(), pre, post, "North", "North")
    assert result is None
