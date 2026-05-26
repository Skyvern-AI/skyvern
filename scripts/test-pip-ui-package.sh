#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WHEELHOUSE="${1:-$(mktemp -d /tmp/skyvern-pip-ui-smoke.XXXXXX)}"
PYTHON_IMAGE="${PYTHON_IMAGE:-python:3.11-slim}"

mkdir -p "$WHEELHOUSE"

cd "$ROOT_DIR"

echo "Building skyvern-ui wheelhouse in $WHEELHOUSE"
scripts/build-skyvern-ui-package.sh "$WHEELHOUSE"
uv build --wheel --out-dir "$WHEELHOUSE"

echo "Checking base skyvern install"
docker run --rm -v "$WHEELHOUSE:/wheelhouse:ro" "$PYTHON_IMAGE" bash -s <<'BASH'
set -euo pipefail
export PIP_FIND_LINKS=/wheelhouse
export PIP_PROGRESS_BAR=off
export PIP_DISABLE_PIP_VERSION_CHECK=1
python -m venv /venv
. /venv/bin/activate
python -m pip install -q --upgrade pip
SKYVERN_WHEEL="$(ls /wheelhouse/skyvern-[0-9]*.whl | head -1)"
python -m pip install -q "$SKYVERN_WHEEL"
python - <<'PY'
import importlib.util
import os
import subprocess
import tempfile

assert importlib.util.find_spec("skyvern_ui") is None
assert importlib.util.find_spec("uvicorn") is None
assert importlib.util.find_spec("starlette") is None

result = subprocess.run(
    ["skyvern", "run", "ui"],
    cwd=tempfile.mkdtemp(),
    env={**os.environ, "TERM": "dumb"},
    text=True,
    capture_output=True,
    timeout=30,
)
output = result.stdout + result.stderr
assert result.returncode == 0, output
assert "Skyvern UI assets are not installed" in output
assert 'pip install "skyvern[ui]"' in output

result = subprocess.run(
    ["skyvern", "run", "all"],
    cwd=tempfile.mkdtemp(),
    env={**os.environ, "TERM": "dumb"},
    text=True,
    capture_output=True,
    timeout=30,
)
output = result.stdout + result.stderr
assert result.returncode == 1, output
assert '`skyvern run all` needs the local server dependencies' in output
assert 'pip install "skyvern[all]"' in output
print("BASE_SMOKE_OK")
PY
BASH

echo "Checking skyvern[ui] install"
docker run --rm -v "$WHEELHOUSE:/wheelhouse:ro" "$PYTHON_IMAGE" bash -s <<'BASH'
set -euo pipefail
export PIP_FIND_LINKS=/wheelhouse
export PIP_PROGRESS_BAR=off
export PIP_DISABLE_PIP_VERSION_CHECK=1
python -m venv /venv
. /venv/bin/activate
python -m pip install -q --upgrade pip
SKYVERN_WHEEL="$(ls /wheelhouse/skyvern-[0-9]*.whl | head -1)"
python -m pip install -q "${SKYVERN_WHEEL}[ui]"
python - <<'PY'
import importlib.util
import os
import subprocess
import tempfile
from pathlib import Path

from skyvern.cli import ui_runtime

assert importlib.util.find_spec("skyvern_ui") is not None
assert importlib.util.find_spec("uvicorn") is None
assert importlib.util.find_spec("starlette") is None
assert ui_runtime.installed_ui_dist_available() is True

cache_dir = Path(tempfile.mkdtemp()) / "cache"
os.environ[ui_runtime.UI_CACHE_ENV_VAR] = str(cache_dir)
runtime = ui_runtime.prepare_installed_ui_dist(
    ui_runtime.InstalledUiConfig(
        api_base_url="http://example.test/api/v1",
        wss_base_url="ws://example.test/api/v1",
        artifact_api_base_url="http://example.test:9090",
        skyvern_api_key="docker-key",
        browser_streaming_mode="cdp",
    )
)
contents = "\n".join(
    path.read_text(errors="ignore")
    for path in runtime.rglob("*")
    if path.is_file() and path.suffix in {".html", ".js", ".css"}
)
assert "__VITE_API_BASE_URL_PLACEHOLDER__" not in contents
assert "__SKYVERN_API_KEY_PLACEHOLDER__" not in contents
assert "http://example.test/api/v1" in contents
assert "docker-key" in contents

try:
    result = subprocess.run(
        ["skyvern", "run", "ui"],
        cwd=tempfile.mkdtemp(),
        env={**os.environ, "TERM": "dumb", "SKYVERN_UI_CACHE_DIR": str(cache_dir / "serve")},
        text=True,
        capture_output=True,
        timeout=5,
    )
    output = result.stdout + result.stderr
except subprocess.TimeoutExpired as exc:
    stdout = exc.stdout or ""
    stderr = exc.stderr or ""
    if isinstance(stdout, bytes):
        stdout = stdout.decode(errors="ignore")
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="ignore")
    output = stdout + stderr
assert "Starting packaged Skyvern UI" in output
print("UI_EXTRA_SMOKE_OK")
PY
BASH

echo "Checking skyvern[server] install"
docker run --rm -v "$WHEELHOUSE:/wheelhouse:ro" "$PYTHON_IMAGE" bash -s <<'BASH'
set -euo pipefail
export PIP_FIND_LINKS=/wheelhouse
export PIP_PROGRESS_BAR=off
export PIP_DISABLE_PIP_VERSION_CHECK=1
python -m venv /venv
. /venv/bin/activate
python -m pip install -q --upgrade pip
SKYVERN_WHEEL="$(ls /wheelhouse/skyvern-[0-9]*.whl | head -1)"
python -m pip install -q "${SKYVERN_WHEEL}[server]"
python - <<'PY'
import importlib.util
import os
import subprocess
import tempfile

assert importlib.util.find_spec("skyvern_ui") is None
assert importlib.util.find_spec("uvicorn") is not None
assert importlib.util.find_spec("starlette") is not None

help_result = subprocess.run(["skyvern", "run", "server", "--help"], text=True, capture_output=True, timeout=30)
assert help_result.returncode == 0, help_result.stderr

result = subprocess.run(
    ["skyvern", "run", "ui"],
    cwd=tempfile.mkdtemp(),
    env={**os.environ, "TERM": "dumb"},
    text=True,
    capture_output=True,
    timeout=30,
)
output = result.stdout + result.stderr
assert result.returncode == 0, output
assert "Skyvern UI assets are not installed" in output
print("SERVER_EXTRA_SMOKE_OK")
PY
BASH

echo "Checking skyvern[all] install"
docker run --rm -v "$WHEELHOUSE:/wheelhouse:ro" "$PYTHON_IMAGE" bash -s <<'BASH'
set -euo pipefail
export PIP_FIND_LINKS=/wheelhouse
export PIP_PROGRESS_BAR=off
export PIP_DISABLE_PIP_VERSION_CHECK=1
python -m venv /venv
. /venv/bin/activate
python -m pip install -q --upgrade pip
SKYVERN_WHEEL="$(ls /wheelhouse/skyvern-[0-9]*.whl | head -1)"
python -m pip install -q "${SKYVERN_WHEEL}[all]"
python - <<'PY'
import importlib.util
import os
import subprocess
import tempfile

from skyvern.cli import ui_runtime

assert importlib.util.find_spec("skyvern_ui") is not None
assert importlib.util.find_spec("uvicorn") is not None
assert importlib.util.find_spec("starlette") is not None
assert ui_runtime.installed_ui_dist_available() is True

help_result = subprocess.run(["skyvern", "run", "all", "--help"], text=True, capture_output=True, timeout=30)
assert help_result.returncode == 0, help_result.stderr
assert "--install-ui" in help_result.stdout

try:
    result = subprocess.run(
        ["skyvern", "run", "ui"],
        cwd=tempfile.mkdtemp(),
        env={**os.environ, "TERM": "dumb", "SKYVERN_UI_CACHE_DIR": tempfile.mkdtemp()},
        text=True,
        capture_output=True,
        timeout=5,
    )
    output = result.stdout + result.stderr
except subprocess.TimeoutExpired as exc:
    stdout = exc.stdout or ""
    stderr = exc.stderr or ""
    if isinstance(stdout, bytes):
        stdout = stdout.decode(errors="ignore")
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="ignore")
    output = stdout + stderr
assert "Starting packaged Skyvern UI" in output
print("ALL_EXTRA_SMOKE_OK")
PY
BASH

echo "Checking auto-install path"
docker run --rm -v "$WHEELHOUSE:/wheelhouse:ro" "$PYTHON_IMAGE" bash -s <<'BASH'
set -euo pipefail
export PIP_FIND_LINKS=/wheelhouse
export PIP_PROGRESS_BAR=off
export PIP_DISABLE_PIP_VERSION_CHECK=1
python -m venv /venv
. /venv/bin/activate
python -m pip install -q --upgrade pip
SKYVERN_WHEEL="$(ls /wheelhouse/skyvern-[0-9]*.whl | head -1)"
python -m pip install -q "$SKYVERN_WHEEL"
python - <<'PY'
import importlib.util
import os
import subprocess
import tempfile

assert importlib.util.find_spec("skyvern_ui") is None
try:
    result = subprocess.run(
        ["skyvern", "run", "ui", "--install-ui"],
        cwd=tempfile.mkdtemp(),
        env={**os.environ, "TERM": "dumb", "PIP_FIND_LINKS": "/wheelhouse", "SKYVERN_UI_CACHE_DIR": tempfile.mkdtemp()},
        text=True,
        capture_output=True,
        timeout=20,
    )
    output = result.stdout + result.stderr
except subprocess.TimeoutExpired as exc:
    stdout = exc.stdout or ""
    stderr = exc.stderr or ""
    if isinstance(stdout, bytes):
        stdout = stdout.decode(errors="ignore")
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="ignore")
    output = stdout + stderr
assert "Installing skyvern-ui==" in output
assert "Packaged Skyvern UI assets installed" in output
assert "Starting packaged Skyvern UI" in output
print("AUTO_INSTALL_SMOKE_OK")
PY
BASH

echo "All pip UI package smoke tests passed."
