from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / ".github/actions/setup-ai-env/uv-sync.sh"


def _make_fake_uv(tmp_path: Path) -> Path:
    uv = tmp_path / "uv"
    uv.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$UV_ARGS_FILE"\n', encoding="utf-8")
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    return uv


def test_setup_ai_env_uv_sync_groups_are_expanded(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _make_fake_uv(fake_bin)
    args_file = tmp_path / "uv_args.txt"

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["UV_SYNC_GROUPS"] = " cloud, dev , ,"
    env["UV_ARGS_FILE"] = str(args_file)

    result = subprocess.run(["bash", str(SCRIPT_PATH)], cwd=REPO_ROOT, env=env, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert args_file.read_text(encoding="utf-8").splitlines() == [
        "sync",
        "--group",
        "cloud",
        "--group",
        "dev",
    ]
