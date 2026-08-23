from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from .result import Artifact


def get_artifact_dir(session_id: str | None = None, run_id: str | None = None) -> Path:
    override = os.environ.get("SKYVERN_MCP_ARTIFACT_DIR")
    if override:
        return Path(override).expanduser() / (session_id or run_id or "anonymous") / "screenshots"
    base = Path.home() / ".skyvern" / "artifacts" / datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if session_id:
        return base / session_id
    if run_id:
        return base / run_id
    return base / "anonymous"


def save_artifact(
    content: bytes,
    kind: str,
    filename: str,
    mime: str,
    session_id: str | None = None,
) -> Artifact:
    dir_path = get_artifact_dir(session_id)
    dir_path.mkdir(parents=True, exist_ok=True)
    file_path = dir_path / filename
    file_path.write_bytes(content)
    return Artifact(kind=kind, path=str(file_path), mime=mime, bytes=len(content))
