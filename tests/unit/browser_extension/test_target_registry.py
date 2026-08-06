from __future__ import annotations

import pytest

from skyvern.browser_extension.target_registry import VirtualTargetRegistry


def test_register_list_and_update_tab_target_info() -> None:
    registry = VirtualTargetRegistry()

    target_id = registry.register_tab(42, "https://example.com/start", "Start")
    root_session_id = registry.root_session_id(42)

    assert target_id == "tab-42"
    assert registry.root_session_id(42) == root_session_id
    assert registry.tab_for_target(target_id) == 42
    assert registry.target_info(target_id) == {
        "targetId": "tab-42",
        "type": "page",
        "title": "Start",
        "url": "https://example.com/start",
        "attached": True,
        "canAccessOpener": False,
        "browserContextId": "skyvern-default",
    }

    registry.update_tab(42, "https://example.com/next", "Next")

    assert registry.list_page_targets() == [
        {
            "targetId": "tab-42",
            "type": "page",
            "title": "Next",
            "url": "https://example.com/next",
            "attached": True,
            "canAccessOpener": False,
            "browserContextId": "skyvern-default",
        }
    ]


def test_root_session_changes_after_remove_and_reregister() -> None:
    registry = VirtualTargetRegistry()
    registry.register_tab(7, "about:blank", "")
    first_session_id = registry.root_session_id(7)

    registry.remove_tab(7)
    registry.register_tab(7, "about:blank", "")

    assert registry.root_session_id(7) != first_session_id


def test_root_session_aliases_are_unique_resolvable_and_independently_removable() -> None:
    registry = VirtualTargetRegistry()
    registry.register_tab(7, "about:blank", "")
    primary_session_id = registry.root_session_id(7)

    first_alias = registry.create_root_session_alias(7)
    second_alias = registry.create_root_session_alias(7)

    assert len({primary_session_id, first_alias, second_alias}) == 3
    assert registry.root_session_ids(7) == [primary_session_id, first_alias, second_alias]
    assert registry.resolve_session(first_alias) == (7, None)
    assert registry.resolve_session(second_alias) == (7, None)

    assert registry.remove_root_session_alias(first_alias)
    assert not registry.remove_root_session_alias(primary_session_id)
    assert registry.root_session_ids(7) == [primary_session_id, second_alias]
    with pytest.raises(KeyError):
        registry.resolve_session(first_alias)


def test_browser_session_aliases_are_unique_and_independently_removable() -> None:
    registry = VirtualTargetRegistry()

    first_alias = registry.create_browser_session_alias()
    second_alias = registry.create_browser_session_alias()

    assert first_alias != second_alias
    assert registry.is_browser_session_alias(first_alias)
    assert registry.is_browser_session_alias(second_alias)
    assert registry.remove_browser_session_alias(first_alias)
    assert not registry.is_browser_session_alias(first_alias)
    assert registry.is_browser_session_alias(second_alias)


def test_child_session_register_resolve_and_remove() -> None:
    registry = VirtualTargetRegistry()
    registry.register_tab(9, "https://example.com", "Example")
    child_target_info = {
        "targetId": "frame-1",
        "type": "iframe",
        "title": "",
        "url": "https://example.com/frame",
        "attached": True,
        "canAccessOpener": False,
        "browserContextId": "skyvern-default",
    }

    registry.register_child_session(9, "chrome-child-1", child_target_info)

    assert registry.resolve_session(registry.root_session_id(9)) == (9, None)
    assert registry.resolve_session("chrome-child-1") == (9, "chrome-child-1")
    assert registry.target_info("frame-1") == child_target_info

    registry.remove_child_session("chrome-child-1")

    with pytest.raises(KeyError):
        registry.resolve_session("chrome-child-1")
    with pytest.raises(KeyError):
        registry.target_info("frame-1")


def test_remove_tab_removes_root_and_child_sessions() -> None:
    registry = VirtualTargetRegistry()
    target_id = registry.register_tab(5, "https://example.com", "Example")
    root_session_id = registry.root_session_id(5)
    root_alias = registry.create_root_session_alias(5)
    registry.register_child_session(5, "chrome-child", {"targetId": "frame-5"})

    registry.remove_tab(5)

    for session_id in (root_session_id, root_alias, "chrome-child"):
        with pytest.raises(KeyError):
            registry.resolve_session(session_id)
    with pytest.raises(KeyError):
        registry.target_info(target_id)
    with pytest.raises(KeyError):
        registry.tab_for_target(target_id)


def test_unknown_registry_entries_raise_key_error() -> None:
    registry = VirtualTargetRegistry()

    with pytest.raises(KeyError):
        registry.root_session_id(99)
    with pytest.raises(KeyError):
        registry.resolve_session("missing-session")
    with pytest.raises(KeyError):
        registry.target_info("missing-target")
    with pytest.raises(KeyError):
        registry.tab_for_target("missing-target")
    with pytest.raises(KeyError):
        registry.update_tab(99, "about:blank", "")
    with pytest.raises(KeyError):
        registry.register_child_session(99, "missing-child", {"targetId": "frame-missing"})


def test_clear_removes_all_registry_state() -> None:
    registry = VirtualTargetRegistry()
    target_id = registry.register_tab(3, "https://example.com", "Example")
    root_session_id = registry.root_session_id(3)
    root_alias = registry.create_root_session_alias(3)
    browser_alias = registry.create_browser_session_alias()
    registry.register_child_session(3, "chrome-child", {"targetId": "frame-3"})

    registry.clear()

    assert registry.list_page_targets() == []
    with pytest.raises(KeyError):
        registry.resolve_session(root_session_id)
    with pytest.raises(KeyError):
        registry.resolve_session(root_alias)
    with pytest.raises(KeyError):
        registry.resolve_session("chrome-child")
    with pytest.raises(KeyError):
        registry.tab_for_target(target_id)
    assert not registry.is_browser_session_alias(browser_alias)
