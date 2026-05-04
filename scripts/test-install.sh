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

# Variant matrix. Today only 'server' is accepted by install.sh; the loop
# scaffolding is in place so that split day is a data-only change — append
# 'sdk', 'mcp', 'all' once their PyPI targets exist.
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
        if docker run --rm -v "$INSTALL_SH:/tmp/install.sh:ro" "$img" sh -c "
            set -e
            export DEBIAN_FRONTEND=noninteractive
            apt-get update -qq >/dev/null
            apt-get install -qq -y curl ca-certificates >/dev/null
            sh /tmp/install.sh --variant '$variant' $init_flag
            export PATH=\"\$HOME/.local/bin:\$PATH\"
            # skyvern's CLI exposes no '--version' flag; --help exiting 0
            # proves the package imports cleanly (including playwright for
            # variants that pull it in).
            skyvern --help >/dev/null
        "; then
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
