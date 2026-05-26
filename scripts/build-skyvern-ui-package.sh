#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_DIR="$ROOT_DIR/skyvern-frontend"
PACKAGE_DIR="$ROOT_DIR/packages/skyvern-ui"
PACKAGE_DIST_DIR="$PACKAGE_DIR/skyvern_ui/dist"
OUT_DIR="${1:-$ROOT_DIR/dist}"

cd "$FRONTEND_DIR"

export VITE_API_BASE_URL="__VITE_API_BASE_URL_PLACEHOLDER__"
export VITE_WSS_BASE_URL="__VITE_WSS_BASE_URL_PLACEHOLDER__"
export VITE_ARTIFACT_API_BASE_URL="__VITE_ARTIFACT_API_BASE_URL_PLACEHOLDER__"
export VITE_SKYVERN_API_KEY="__SKYVERN_API_KEY_PLACEHOLDER__"
export VITE_BROWSER_STREAMING_MODE="__VITE_BROWSER_STREAMING_MODE_PLACEHOLDER__"

npm ci
npm run build

rm -rf "$PACKAGE_DIST_DIR"
mkdir -p "$PACKAGE_DIST_DIR"
cp -R "$FRONTEND_DIR/dist/." "$PACKAGE_DIST_DIR/"

cd "$ROOT_DIR"
uv build "$PACKAGE_DIR" --out-dir "$OUT_DIR"

python - "$OUT_DIR" "$PACKAGE_DIR" <<'PY'
from pathlib import Path
import sys
import tomllib
import zipfile

out_dir = Path(sys.argv[1])
package_dir = Path(sys.argv[2])
version = tomllib.loads((package_dir / "pyproject.toml").read_text())["project"]["version"]
wheel = out_dir / f"skyvern_ui-{version}-py3-none-any.whl"

if not wheel.is_file():
    raise SystemExit(f"No skyvern-ui wheel found at {wheel}")
with zipfile.ZipFile(wheel) as archive:
    names = set(archive.namelist())

if "skyvern_ui/dist/index.html" not in names:
    raise SystemExit(f"{wheel} is missing skyvern_ui/dist/index.html")
if not any(name.startswith("skyvern_ui/dist/assets/") for name in names):
    raise SystemExit(f"{wheel} is missing skyvern_ui/dist/assets")

print(f"Verified prebuilt UI assets in {wheel}")
PY
