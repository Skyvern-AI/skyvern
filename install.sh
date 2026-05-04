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

# Empty SKYVERN_RUN_INIT means "use the variant's default"; --no-init or
# `SKYVERN_RUN_INIT=0` overrides to 0; `SKYVERN_RUN_INIT=1` forces it on.
SKYVERN_RUN_INIT="${SKYVERN_RUN_INIT:-}"
SKYVERN_VERSION="${SKYVERN_VERSION:-}"
SKYVERN_VARIANT="${SKYVERN_VARIANT:-server}"

while [ $# -gt 0 ]; do
    case "$1" in
        --no-init)   SKYVERN_RUN_INIT=0; shift ;;
        --version)   SKYVERN_VERSION="${2:?--version requires a value}"; shift 2 ;;
        --version=*) SKYVERN_VERSION="${1#--version=}"; shift ;;
        --variant)   SKYVERN_VARIANT="${2:?--variant requires a value}"; shift 2 ;;
        --variant=*) SKYVERN_VARIANT="${1#--variant=}"; shift ;;
        --help|-h)
            # Self-parsing of "$0" doesn't work when piped (`curl ... | sh`);
            # `$0` is then the shell name, not a real file. Embed help inline.
            cat <<'EOF'
Skyvern installer.

Usage:
  curl -LsSf https://install.skyvern.com | sh
  curl -LsSf https://install.skyvern.com | sh -s -- --no-init
  curl -LsSf https://install.skyvern.com | sh -s -- --version 1.0.31
  curl -LsSf https://install.skyvern.com | sh -s -- --variant server

Flags:
  --variant V    install variant (default: server). Other variants
                 (sdk, mcp, all) are reserved for the upcoming SDK
                 package split — currently only 'server' is accepted.
  --no-init      skip 'skyvern init' (the Chromium browser install).
  --version X    pin a specific PyPI version (e.g., 1.0.31).

Environment overrides:
  SKYVERN_VARIANT=V    same as --variant
  SKYVERN_RUN_INIT=0   same as --no-init
  SKYVERN_VERSION=X    same as --version

What this does:
  1. Bootstraps 'uv' (Astral's Python toolchain) if not already installed.
     uv handles Python version management — the host machine doesn't need
     a pre-installed Python.
  2. Installs the matching PyPI package as an isolated tool, exposing
     the 'skyvern' command on PATH.
  3. For variants that need a browser, runs 'skyvern init' to download
     Chromium (skip with --no-init when running in CI / minimal containers).
EOF
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
    # Download then execute, rather than `curl ... | sh`. POSIX /bin/sh has no
    # `pipefail`, so a curl failure (captive portal, MITM truncation, 5xx)
    # would otherwise be silently swallowed and the script would carry on.
    UV_INSTALLER="$(mktemp -t skyvern-uv-installer.XXXXXX)"
    if ! curl -LsSf https://astral.sh/uv/install.sh -o "$UV_INSTALLER"; then
        rm -f "$UV_INSTALLER"
        printf 'ERROR: failed to download uv installer from astral.sh.\n' >&2
        printf '       Check your network/proxy. See https://docs.astral.sh/uv/getting-started/installation/\n' >&2
        exit 1
    fi
    sh "$UV_INSTALLER"
    rm -f "$UV_INSTALLER"
    # uv drops binaries in ~/.local/bin and updates shell rc files for future
    # sessions. Update PATH here so the rest of this script can call uv
    # directly, then sanity-check the binary actually exists.
    export PATH="$HOME/.local/bin:$PATH"
    if ! command -v uv >/dev/null 2>&1; then
        printf 'ERROR: uv installer ran but uv is not on PATH.\n' >&2
        printf '       See https://docs.astral.sh/uv/getting-started/installation/\n' >&2
        exit 1
    fi
fi

# Variant policy table. Each variant decides:
#   PKG_SPEC          which PyPI spec to install
#   PYTHON_SPEC       which Python range to pin (PEP 440 / 508 specifier)
#   DEFAULT_RUN_INIT  whether 'skyvern init' (Chromium) runs by default
#   SUCCESS_HINT      the "what to try next" line printed on success
#
# Today only 'server' is accepted. The Skyvern SDK package split (per the
# March 2026 PRD) will introduce additional variants; when their PyPI
# targets exist, add cases below — no other code change needed:
#   sdk: lightweight `skyvern` (no Chromium, no server). DEFAULT_RUN_INIT=0.
#   mcp: standalone `skyvern-mcp` (SKY-7946).
#   all: backward-compat alias for today's monolithic install.
case "$SKYVERN_VARIANT" in
    server)
        # Pre-split: monolithic `skyvern` already includes server, browsers,
        # and LLM clients. Post-split: change to `skyvern[server]`.
        PKG_SPEC="skyvern"
        PYTHON_SPEC=">=3.11,<3.14"
        DEFAULT_RUN_INIT=1
        SUCCESS_HINT="Try: skyvern --help"
        ;;
    sdk|mcp|cli|cloud|all)
        printf "ERROR: --variant '%s' is reserved for the upcoming SDK package split\n" "$SKYVERN_VARIANT" >&2
        printf "       and is not yet available. Use --variant server (the default) for now.\n" >&2
        exit 2
        ;;
    *)
        printf "ERROR: unknown variant '%s'. Valid: server (default).\n" "$SKYVERN_VARIANT" >&2
        exit 2
        ;;
esac

# Apply --no-init / SKYVERN_RUN_INIT user override; otherwise use variant default.
[ -n "$SKYVERN_RUN_INIT" ] || SKYVERN_RUN_INIT="$DEFAULT_RUN_INIT"

PKG_SPEC_INSTALL="$PKG_SPEC"
if [ -n "$SKYVERN_VERSION" ]; then
    PKG_SPEC_INSTALL="${PKG_SPEC}==${SKYVERN_VERSION}"
fi

printf 'installing %s via uv tool install...\n' "$PKG_SPEC_INSTALL"
# PYTHON_SPEC is hardcoded per variant. Without this, uv may pick a host
# Python outside the supported range (e.g., 3.14), in which case
# environment-marker dependencies like playwright (`<'3.14'`) silently drop
# out of the resolution and the CLI crashes at import time. Bump this range
# intentionally when the upstream `requires-python` shifts — it does NOT
# auto-track. (Filed as a follow-up: CI job that diffs PYTHON_SPEC against
# the published `requires-python` for the variant.)
uv tool install --python "$PYTHON_SPEC" --force "$PKG_SPEC_INSTALL"

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

printf '\nSkyvern installed. %s\n' "$SUCCESS_HINT"

if [ "$uv_was_freshly_installed" = "1" ]; then
    printf "\nNote: uv was just installed in ~/.local/bin. To use 'skyvern' in\n"
    printf '      your current shell session, run:\n'
    printf '          source ~/.local/bin/env\n'
    printf '      Or open a new terminal (uv updated your shell rc).\n'
fi
