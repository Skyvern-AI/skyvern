import json
import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

MAX_ENTRIES = 200
# Budget for trajectory_json AS EMBEDDED in the tool response: _json_size measures the
# double-encoded length (the string re-escapes when the response dict is serialized),
# so this bounds the final on-the-wire cost against MCP_MAX_RESPONSE_CHARS (140k)
# with headroom for the rest of the envelope.
MAX_BYTES = 120_000
TTL_SECONDS = 60 * 60
MAX_SESSIONS = 64
# One tenant minting many sessions must not flush other tenants' slots: past this,
# the tenant self-evicts its own least-recently-touched session first.
MAX_SESSIONS_PER_TENANT = 16


@dataclass
class _Trajectory:
    entries: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    last_touch: float = 0.0


_trajectories: dict[tuple[str | None, str], _Trajectory] = {}


def _sweep_expired(now: float) -> None:
    expired = [key for key, trajectory in _trajectories.items() if now - trajectory.last_touch > TTL_SECONDS]
    for key in expired:
        _trajectories.pop(key, None)


def _json_size(entries: list[dict[str, Any]]) -> int:
    # char count == byte upper bound only while json.dumps keeps its ensure_ascii=True default.
    return len(json.dumps(json.dumps(entries)))


def append_trajectory_entry(*, api_key_hash: str | None, session_id: str, entry: dict[str, Any]) -> None:
    stored_entry = deepcopy(entry)
    entry_size = _json_size([stored_entry])
    now = time.monotonic()
    _sweep_expired(now)
    key = (api_key_hash, session_id)
    previous = _trajectories.get(key)
    entries = list(previous.entries) if previous else []
    truncated = previous.truncated if previous else False

    if entry_size > MAX_BYTES:
        truncated = True
    else:
        entries.append(stored_entry)
        while len(entries) > MAX_ENTRIES or _json_size(entries) > MAX_BYTES:
            entries.pop(0)
            truncated = True

    _trajectories.pop(key, None)
    _trajectories[key] = _Trajectory(entries=entries, truncated=truncated, last_touch=now)
    tenant_keys = [stored_key for stored_key in _trajectories if stored_key[0] == api_key_hash]
    while len(tenant_keys) > MAX_SESSIONS_PER_TENANT:
        _trajectories.pop(tenant_keys.pop(0))
    while len(_trajectories) > MAX_SESSIONS:
        _trajectories.pop(next(iter(_trajectories)))


def get_trajectory(*, api_key_hash: str | None, session_id: str) -> tuple[list[dict[str, Any]], bool, bool]:
    now = time.monotonic()
    _sweep_expired(now)
    key = (api_key_hash, session_id)
    trajectory = _trajectories.pop(key, None)
    if trajectory is None:
        return [], False, False

    trajectory.last_touch = now
    _trajectories[key] = trajectory
    return deepcopy(trajectory.entries), trajectory.truncated, True


def delete_trajectory(*, api_key_hash: str | None, session_id: str) -> None:
    _trajectories.pop((api_key_hash, session_id), None)


def delete_session_trajectories(session_id: str) -> None:
    # A closed browser session invalidates every principal's capture for it (same-org
    # drivers may hold different API keys); callers gate this on a backend-authorized close.
    for key in [stored_key for stored_key in _trajectories if stored_key[1] == session_id]:
        _trajectories.pop(key, None)
