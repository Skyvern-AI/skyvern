#!/bin/sh
# Smoke-test install.sh against fresh Docker base images.
#
# Usage:
#   scripts/test-install.sh                # default matrix, --no-init
#   scripts/test-install.sh --with-init    # run post-install setup when possible
#   IMAGES="ubuntu:22.04" scripts/test-install.sh   # subset
#
# Requires Docker on the host.

set -eu

WITH_INIT=0
[ "${1:-}" = "--with-init" ] && WITH_INIT=1

DEFAULT_IMAGES="ubuntu:22.04 ubuntu:24.04 debian:12-slim python:3.11-slim python:3.13-slim"
IMAGES="${IMAGES:-$DEFAULT_IMAGES}"

# This installer is intentionally server-only; SDK/local installs belong in a
# project environment via pip, not a uv tool venv.
DEFAULT_VARIANTS="server"
VARIANTS="${VARIANTS:-$DEFAULT_VARIANTS}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_SH="$REPO_ROOT/install.sh"
[ -f "$INSTALL_SH" ] || { printf 'install.sh not found at %s\n' "$INSTALL_SH" >&2; exit 1; }

init_flag="--no-init"
[ "$WITH_INIT" = "1" ] && init_flag=""

failed=""
for variant in $VARIANTS; do
    for img in $IMAGES; do
        printf '\n=== %s / variant=%s ===\n' "$img" "$variant"
        if docker run --rm \
            -v "$INSTALL_SH:/tmp/install.sh:ro" \
            -e "VARIANT=$variant" \
            -e "INIT_FLAG=$init_flag" \
            "$img" sh -c '
            set -e
            export DEBIAN_FRONTEND=noninteractive
            apt-get update -qq >/dev/null
            apt-get install -qq -y curl ca-certificates >/dev/null
            sh /tmp/install.sh --variant "$VARIANT" $INIT_FLAG
            export PATH="$HOME/.local/bin:$PATH"
            # --help exit 0 proves the package imports cleanly (the CLI
            # has no --version flag).
            skyvern --help >/dev/null
        '; then
            printf 'PASS: %s / variant=%s\n' "$img" "$variant"
        else
            printf 'FAIL: %s / variant=%s\n' "$img" "$variant"
            failed="$failed $img(variant=$variant)"
        fi
    done
done

if [ -n "$failed" ]; then
    printf '\nFAILED:%s\n' "$failed" >&2
    exit 1
fi

printf '\nAll images passed.\n'
