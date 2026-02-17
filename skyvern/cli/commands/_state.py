from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path.home() / ".skyvern"
STATE_FILE = STATE_DIR / "state.json"

_TTL_SECONDS = 86400  # 24 hours


@dataclass
class CLIState:
    session_id: str | None = None
    cdp_url: str | None = None
    mode: str | None = None  # "cloud", "local", or "cdp"
    created_at: str | None = None


def save_state(state: CLIState) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.chmod(0o700)
    data = asdict(state)
    data["created_at"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.write_text(json.dumps(data))
    STATE_FILE.chmod(0o600)


def load_state() -> CLIState | None:
    if not STATE_FILE.exists():
        return None
    try:
        data = json.loads(STATE_FILE.read_text())
        created_at = data.get("created_at")
        if created_at:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(created_at)).total_seconds()
            if age > _TTL_SECONDS:
                return None
        return CLIState(**{k: v for k, v in data.items() if k in CLIState.__dataclass_fields__})
    except Exception:
        return None


def clear_state() -> None:
    if STATE_FILE.exists():
        STATE_FILE.unlink()
