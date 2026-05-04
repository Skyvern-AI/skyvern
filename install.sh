#!/bin/sh
# Skyvern installer.
#
# Usage:
#   curl -LsSf https://install.skyvern.com | sh
#   curl -LsSf https://install.skyvern.com | sh -s -- --no-init
#   curl -LsSf https://install.skyvern.com | sh -s -- --version 1.0.31
#
# Environment overrides:
#   SKYVERN_RUN_INIT=0   skip `skyvern init` (Chromium browser install)
#   SKYVERN_VERSION=X    pin a specific PyPI version
#
# What this does:
#   1. Bootstraps `uv` (Astral's Python toolchain) if not already installed.
#      uv handles Python version management — the host machine doesn't need
#      a pre-installed Python.
#   2. Installs the `skyvern` package from PyPI as an isolated tool, exposing
#      the `skyvern` command on PATH.
#   3. Runs `skyvern init` to install Chromium for browser automation
#      (skip with --no-init when running in CI / minimal containers).

set -eu

SKYVERN_RUN_INIT="${SKYVERN_RUN_INIT:-1}"
SKYVERN_VERSION="${SKYVERN_VERSION:-}"

while [ $# -gt 0 ]; do
    case "$1" in
        --no-init)   SKYVERN_RUN_INIT=0; shift ;;
        --version)   SKYVERN_VERSION="${2:?--version requires a value}"; shift 2 ;;
        --version=*) SKYVERN_VERSION="${1#--version=}"; shift ;;
        --help|-h)
            sed -n '2,20p' "$0" | sed -n 's/^# \{0,1\}//p'
            exit 0
            ;;
        *)
            printf 'unknown option: %s (try --help)\n' "$1" >&2
            exit 2
            ;;
    esac
done

case "$(uname -s)" in
    Darwin|Linux) ;;
    *)
        printf 'unsupported OS: %s\n' "$(uname -s)" >&2
        printf "For Windows, use WSL or 'pip install skyvern' from a Python environment.\n" >&2
        exit 1
        ;;
esac

uv_was_freshly_installed=0
if ! command -v uv >/dev/null 2>&1; then
    uv_was_freshly_installed=1
    printf 'installing uv (one-time, into ~/.local/bin)...\n'
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # The uv installer drops binaries in ~/.local/bin and updates the user's
    # shell rc files for future sessions. We update PATH here so the rest of
    # this script can call uv directly.
    export PATH="$HOME/.local/bin:$PATH"
fi

# When the SDK package split lands (lightweight `skyvern` SDK + full
# `skyvern[server]` + standalone `skyvern-mcp`), change the next line to:
#     PKG_SPEC="skyvern[server]"
# until then, the monolithic `skyvern` package contains the full server.
PKG_SPEC="skyvern"
if [ -n "$SKYVERN_VERSION" ]; then
    PKG_SPEC="${PKG_SPEC}==${SKYVERN_VERSION}"
fi

printf 'installing %s via uv tool install...\n' "$PKG_SPEC"
uv tool install --force "$PKG_SPEC"

if [ "$SKYVERN_RUN_INIT" = "1" ]; then
    printf "\nrunning 'skyvern init' (installs Chromium, ~150MB)...\n"
    if ! skyvern init; then
        # Chromium download can fail behind corporate proxies or on flaky
        # networks. The CLI is installed regardless; surface the recovery path
        # instead of returning non-zero from the whole installer.
        printf "\nWARN: 'skyvern init' did not complete. The CLI is installed;\n" >&2
        printf "      re-run 'skyvern init' once your network/proxy is sorted.\n" >&2
    fi
fi

printf '\nSkyvern installed. Try: skyvern --help\n'

if [ "$uv_was_freshly_installed" = "1" ]; then
    printf "\nNote: uv was just installed in ~/.local/bin. To use 'skyvern' in\n"
    printf '      your current shell session, run:\n'
    printf '          source ~/.local/bin/env\n'
    printf '      Or open a new terminal (uv updated your shell rc).\n'
fi
