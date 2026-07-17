from collections.abc import Iterator

import pytest

from skyvern.cli.core import trajectory_store


@pytest.fixture(autouse=True)
def reset_trajectory_store() -> Iterator[None]:
    trajectory_store._trajectories.clear()
    yield
    trajectory_store._trajectories.clear()


def test_append_and_get_trajectory() -> None:
    entry = {"tool_name": "click", "selector": "#submit"}

    trajectory_store.append_trajectory_entry(api_key_hash="hash-a", session_id="session-1", entry=entry)

    assert trajectory_store.get_trajectory(api_key_hash="hash-a", session_id="session-1") == ([entry], False, True)
    assert trajectory_store.get_trajectory(api_key_hash="hash-a", session_id="unknown") == ([], False, False)


def test_entry_count_overflow_drops_oldest_and_stays_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trajectory_store, "MAX_ENTRIES", 2)

    for index in range(3):
        trajectory_store.append_trajectory_entry(
            api_key_hash="hash-a",
            session_id="session-1",
            entry={"index": index},
        )

    assert trajectory_store.get_trajectory(api_key_hash="hash-a", session_id="session-1") == (
        [{"index": 1}, {"index": 2}],
        True,
        True,
    )

    trajectory_store.append_trajectory_entry(
        api_key_hash="hash-a",
        session_id="session-1",
        entry={"index": 3},
    )
    assert trajectory_store.get_trajectory(api_key_hash="hash-a", session_id="session-1") == (
        [{"index": 2}, {"index": 3}],
        True,
        True,
    )


def test_byte_overflow_drops_oldest(monkeypatch: pytest.MonkeyPatch) -> None:
    first = {"value": "first"}
    second = {"value": "second"}
    max_single_entry_bytes = max(
        trajectory_store._json_size([first]),
        trajectory_store._json_size([second]),
    )
    monkeypatch.setattr(trajectory_store, "MAX_BYTES", max_single_entry_bytes)

    trajectory_store.append_trajectory_entry(api_key_hash="hash-a", session_id="session-1", entry=first)
    trajectory_store.append_trajectory_entry(api_key_hash="hash-a", session_id="session-1", entry=second)

    assert trajectory_store.get_trajectory(api_key_hash="hash-a", session_id="session-1") == ([second], True, True)


def test_single_oversize_entry_is_dropped_and_sets_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    small = {"value": "ok"}
    oversized = {"value": "x" * 100}
    monkeypatch.setattr(trajectory_store, "MAX_BYTES", trajectory_store._json_size([small]))

    trajectory_store.append_trajectory_entry(api_key_hash=None, session_id="session-1", entry=oversized)
    assert trajectory_store.get_trajectory(api_key_hash=None, session_id="session-1") == ([], True, True)

    trajectory_store.append_trajectory_entry(api_key_hash=None, session_id="session-1", entry=small)
    assert trajectory_store.get_trajectory(api_key_hash=None, session_id="session-1") == ([small], True, True)


def test_single_oversize_entry_preserves_existing_history(monkeypatch: pytest.MonkeyPatch) -> None:
    history = [{"value": "first"}, {"value": "second"}]
    oversized = {"value": "x" * 100}
    monkeypatch.setattr(trajectory_store, "MAX_BYTES", trajectory_store._json_size(history))

    for entry in history:
        trajectory_store.append_trajectory_entry(api_key_hash="hash-a", session_id="session-1", entry=entry)
    trajectory_store.append_trajectory_entry(api_key_hash="hash-a", session_id="session-1", entry=oversized)

    assert trajectory_store.get_trajectory(api_key_hash="hash-a", session_id="session-1") == (history, True, True)


def test_entries_are_copied_on_append_and_get() -> None:
    entry = {"nested": {"values": [1]}}
    trajectory_store.append_trajectory_entry(api_key_hash="hash-a", session_id="session-1", entry=entry)

    entry["nested"]["values"].append(2)
    returned, _, found = trajectory_store.get_trajectory(api_key_hash="hash-a", session_id="session-1")
    assert found is True
    assert returned == [{"nested": {"values": [1]}}]

    returned[0]["nested"]["values"].append(3)
    assert trajectory_store.get_trajectory(api_key_hash="hash-a", session_id="session-1") == (
        [{"nested": {"values": [1]}}],
        False,
        True,
    )


def test_serialization_error_preserves_existing_history() -> None:
    entry = {"value": "kept"}
    trajectory_store.append_trajectory_entry(api_key_hash="hash-a", session_id="session-1", entry=entry)

    with pytest.raises(TypeError):
        trajectory_store.append_trajectory_entry(
            api_key_hash="hash-a",
            session_id="session-1",
            entry={"value": object()},
        )

    assert trajectory_store.get_trajectory(api_key_hash="hash-a", session_id="session-1") == ([entry], False, True)


def test_ttl_uses_last_touch_and_sweeps_on_append_and_get(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr(trajectory_store, "TTL_SECONDS", 10)
    monkeypatch.setattr(trajectory_store.time, "monotonic", lambda: now[0])

    trajectory_store.append_trajectory_entry(api_key_hash="hash-a", session_id="active", entry={"value": 1})
    trajectory_store.append_trajectory_entry(api_key_hash="hash-a", session_id="stale", entry={"value": 2})

    now[0] = 109.0
    assert trajectory_store.get_trajectory(api_key_hash="hash-a", session_id="active") == (
        [{"value": 1}],
        False,
        True,
    )

    now[0] = 111.0
    trajectory_store.append_trajectory_entry(api_key_hash="hash-a", session_id="fresh", entry={"value": 3})
    assert ("hash-a", "stale") not in trajectory_store._trajectories
    assert trajectory_store.get_trajectory(api_key_hash="hash-a", session_id="active") == (
        [{"value": 1}],
        False,
        True,
    )

    now[0] = 122.0
    assert trajectory_store.get_trajectory(api_key_hash="hash-a", session_id="active") == ([], False, False)


def test_delete_trajectory() -> None:
    trajectory_store.append_trajectory_entry(api_key_hash="hash-a", session_id="session-1", entry={"value": 1})

    trajectory_store.delete_trajectory(api_key_hash="hash-a", session_id="session-1")

    assert trajectory_store.get_trajectory(api_key_hash="hash-a", session_id="session-1") == ([], False, False)


def test_same_session_id_is_isolated_by_api_key_hash() -> None:
    trajectory_store.append_trajectory_entry(
        api_key_hash="hash-a",
        session_id="shared-session",
        entry={"owner": "a"},
    )

    assert trajectory_store.get_trajectory(api_key_hash="hash-b", session_id="shared-session") == ([], False, False)
    assert trajectory_store.get_trajectory(api_key_hash=None, session_id="shared-session") == ([], False, False)
    assert trajectory_store.get_trajectory(api_key_hash="hash-a", session_id="shared-session") == (
        [{"owner": "a"}],
        False,
        True,
    )


def test_per_tenant_cap_self_evicts_without_touching_other_tenants(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr(trajectory_store, "MAX_SESSIONS_PER_TENANT", 2)
    monkeypatch.setattr(trajectory_store.time, "monotonic", lambda: now[0])

    trajectory_store.append_trajectory_entry(api_key_hash="hash-b", session_id="other-tenant", entry={"value": 0})
    for index in range(3):
        now[0] += 1.0
        trajectory_store.append_trajectory_entry(
            api_key_hash="hash-a",
            session_id=f"session-{index}",
            entry={"value": index},
        )

    assert trajectory_store.get_trajectory(api_key_hash="hash-a", session_id="session-0") == ([], False, False)
    assert trajectory_store.get_trajectory(api_key_hash="hash-a", session_id="session-1") == (
        [{"value": 1}],
        False,
        True,
    )
    assert trajectory_store.get_trajectory(api_key_hash="hash-a", session_id="session-2") == (
        [{"value": 2}],
        False,
        True,
    )
    assert trajectory_store.get_trajectory(api_key_hash="hash-b", session_id="other-tenant") == (
        [{"value": 0}],
        False,
        True,
    )


def test_global_cap_evicts_least_recently_touched_session(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr(trajectory_store, "MAX_SESSIONS", 2)
    monkeypatch.setattr(trajectory_store.time, "monotonic", lambda: now[0])

    trajectory_store.append_trajectory_entry(api_key_hash="hash-a", session_id="session-1", entry={"value": 1})
    now[0] = 101.0
    trajectory_store.append_trajectory_entry(api_key_hash="hash-b", session_id="session-2", entry={"value": 2})
    now[0] = 102.0
    trajectory_store.get_trajectory(api_key_hash="hash-a", session_id="session-1")
    now[0] = 103.0
    trajectory_store.append_trajectory_entry(api_key_hash="hash-c", session_id="session-3", entry={"value": 3})

    assert trajectory_store.get_trajectory(api_key_hash="hash-b", session_id="session-2") == ([], False, False)
    assert trajectory_store.get_trajectory(api_key_hash="hash-a", session_id="session-1") == (
        [{"value": 1}],
        False,
        True,
    )
    assert trajectory_store.get_trajectory(api_key_hash="hash-c", session_id="session-3") == (
        [{"value": 3}],
        False,
        True,
    )
