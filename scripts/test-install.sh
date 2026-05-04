#!/bin/sh
# Smoke-test install.sh against fresh Docker base images.
#
# Verifies the curl installer works on machines with:
#   - no Python preinstalled (ubuntu:22.04, ubuntu:24.04, debian:12-slim)
#   - Python preinstalled but no uv (python:3.11-slim, python:3.13-slim)
#
# Usage:
#   scripts/test-install.sh                # default matrix, --no-init
#   scripts/test-install.sh --with-init    # full e2e including Chromium download
#   IMAGES="ubuntu:22.04" scripts/test-install.sh   # subset
#
# Requires Docker on the host.

set -eu

WITH_INIT=0
[ "${1:-}" = "--with-init" ] && WITH_INIT=1

DEFAULT_IMAGES="ubuntu:22.04 ubuntu:24.04 debian:12-slim python:3.11-slim python:3.13-slim"
IMAGES="${IMAGES:-$DEFAULT_IMAGES}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_SH="$REPO_ROOT/install.sh"
[ -f "$INSTALL_SH" ] || { printf 'install.sh not found at %s\n' "$INSTALL_SH" >&2; exit 1; }

init_flag="--no-init"
[ "$WITH_INIT" = "1" ] && init_flag=""

failed=""
for img in $IMAGES; do
    printf '\n=== %s ===\n' "$img"
    if docker run --rm -v "$INSTALL_SH:/tmp/install.sh:ro" "$img" sh -c "
        set -e
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq >/dev/null
        apt-get install -qq -y curl ca-certificates >/dev/null
        sh /tmp/install.sh $init_flag
        export PATH=\"\$HOME/.local/bin:\$PATH\"
        skyvern --version
    "; then
        printf 'PASS: %s\n' "$img"
    else
        printf 'FAIL: %s\n' "$img"
        failed="$failed $img"
    fi
done

if [ -n "$failed" ]; then
    printf '\nFAILED:%s\n' "$failed" >&2
    exit 1
fi

printf '\nAll images passed.\n'
