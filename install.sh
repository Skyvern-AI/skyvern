#!/bin/sh
# Skyvern installer. Run `install.sh --help` for usage.

set -eu

SKYVERN_RUN_INIT="${SKYVERN_RUN_INIT:-}"
SKYVERN_VERSION="${SKYVERN_VERSION:-}"
SKYVERN_VARIANT="${SKYVERN_VARIANT:-server}"

while [ $# -gt 0 ]; do
    case "$1" in
        --no-init)   SKYVERN_RUN_INIT=0; shift ;;
        --run-init|--run-setup) SKYVERN_RUN_INIT=1; shift ;;
        --version)   SKYVERN_VERSION="${2:?--version requires a value}"; shift 2 ;;
        --version=*) SKYVERN_VERSION="${1#--version=}"; shift ;;
        --variant)   SKYVERN_VARIANT="${2:?--variant requires a value}"; shift 2 ;;
        --variant=*) SKYVERN_VARIANT="${1#--variant=}"; shift ;;
        --help|-h)
            # Heredoc rather than self-parsing $0: when piped, $0 is the
            # shell name, not a real file path.
            cat <<'EOF'
Skyvern installer.

Usage:
  curl -LsSf https://install.skyvern.com | sh
  curl -LsSf https://install.skyvern.com | sh -s -- --no-init
  curl -LsSf https://install.skyvern.com | sh -s -- --run-setup
  curl -LsSf https://install.skyvern.com | sh -s -- --version 1.0.35
  curl -LsSf https://install.skyvern.com | sh -s -- --variant server

Flags:
  --variant V    install variant (default: server). This curl installer is
                 intentionally for the self-hosted CLI/server path. For SDKs,
                 use pip inside your project: 'skyvern' or 'skyvern[local]'.
  --run-setup    run the setup wizard after install (requires a terminal).
  --run-init     alias for --run-setup.
  --no-init      skip the post-install setup wizard.
  --version X    pin a specific PyPI version (e.g., 1.0.35).

Environment overrides:
  SKYVERN_VARIANT=V    same as --variant
  SKYVERN_RUN_INIT=0   same as --no-init
  SKYVERN_RUN_INIT=1   same as --run-setup
  SKYVERN_VERSION=X    same as --version

What this does:
  1. Bootstraps 'uv' if missing — uv manages Python so the host
     doesn't need one preinstalled.
  2. Installs the self-hosted PyPI package as an isolated tool
     ('skyvern' on PATH).
  3. Prints the next setup command. Use --run-setup to run it immediately.
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
    # Download then execute (not `curl ... | sh`): POSIX /bin/sh has no
    # pipefail, so curl failures would otherwise be swallowed silently.
    UV_INSTALLER="$(mktemp -t skyvern-uv-installer.XXXXXX)"
    trap 'rm -f "$UV_INSTALLER"' EXIT
    if ! curl -LsSf https://astral.sh/uv/install.sh -o "$UV_INSTALLER"; then
        printf 'ERROR: failed to download uv installer from astral.sh.\n' >&2
        printf '       Check your network/proxy. See https://docs.astral.sh/uv/getting-started/installation/\n' >&2
        exit 1
    fi
    sh "$UV_INSTALLER"
    export PATH="$HOME/.local/bin:$PATH"
    if ! command -v uv >/dev/null 2>&1; then
        printf 'ERROR: uv installer ran but uv is not on PATH.\n' >&2
        printf '       See https://docs.astral.sh/uv/getting-started/installation/\n' >&2
        exit 1
    fi
fi

# Variant policy: the curl installer is for isolated CLI/server installs.
# SDK installs belong in the user's project environment, not a uv tool venv.
case "$SKYVERN_VARIANT" in
    server)
        PKG_SPEC="skyvern[server]"
        PYTHON_SPEC=">=3.11,<3.14"
        DEFAULT_RUN_INIT=0
        SUCCESS_HINT="Next: skyvern quickstart"
        ;;
    sdk|cloud|base)
        printf "ERROR: --variant '%s' is not installed by this curl installer.\n" "$SKYVERN_VARIANT" >&2
        printf "       Use 'pip install skyvern' inside your Python project instead.\n" >&2
        exit 2
        ;;
    local|embedded)
        printf "ERROR: --variant '%s' is not installed by this curl installer.\n" "$SKYVERN_VARIANT" >&2
        printf "       Use 'pip install \"skyvern[local]\"' inside your Python project instead.\n" >&2
        exit 2
        ;;
    mcp|all)
        printf "ERROR: --variant '%s' is not available from this installer.\n" "$SKYVERN_VARIANT" >&2
        printf "       Use --variant server for the self-hosted CLI/server tool.\n" >&2
        exit 2
        ;;
    *)
        printf "ERROR: unknown variant '%s'. Valid: server (default).\n" "$SKYVERN_VARIANT" >&2
        exit 2
        ;;
esac

[ -n "$SKYVERN_RUN_INIT" ] || SKYVERN_RUN_INIT="$DEFAULT_RUN_INIT"
case "$SKYVERN_RUN_INIT" in
    0|1) ;;
    *)
        printf "ERROR: SKYVERN_RUN_INIT must be 0 or 1.\n" >&2
        exit 2
        ;;
esac

PKG_SPEC_INSTALL="$PKG_SPEC"
if [ -n "$SKYVERN_VERSION" ]; then
    PKG_SPEC_INSTALL="${PKG_SPEC}==${SKYVERN_VERSION}"
fi

printf 'installing %s via uv tool install...\n' "$PKG_SPEC_INSTALL"
# Pin Python to the variant's supported range. Without this, uv may pick a
# host Python outside the range and env-marker deps (e.g., playwright with
# `<'3.14'`) silently drop from the resolution. Bump intentionally when
# upstream `requires-python` shifts — does NOT auto-track.
uv tool install --python "$PYTHON_SPEC" --force "$PKG_SPEC_INSTALL"

SETUP_COMMAND="skyvern quickstart"
if skyvern quickstart --help 2>/dev/null | grep -q -- "--install-type"; then
    SETUP_COMMAND="skyvern quickstart --install-type server"
fi
SUCCESS_HINT="Next: $SETUP_COMMAND"

if [ "$SKYVERN_RUN_INIT" = "1" ]; then
    if [ ! -t 0 ]; then
        printf "\nWARN: not running setup because stdin is not an interactive terminal.\n" >&2
        printf "      Run '%s' from your shell when ready.\n" "$SETUP_COMMAND" >&2
    else
        printf "\nrunning '%s'...\n" "$SETUP_COMMAND"
        if ! sh -c "$SETUP_COMMAND"; then
            printf "\nWARN: '%s' did not complete. The CLI is installed;\n" "$SETUP_COMMAND" >&2
            printf "      re-run '%s' once the issue is sorted.\n" "$SETUP_COMMAND" >&2
        fi
    fi
fi

printf '\nSkyvern installed. %s\n' "$SUCCESS_HINT"

if [ "$uv_was_freshly_installed" = "1" ]; then
    printf "\nNote: uv was just installed in ~/.local/bin. Run 'source ~/.local/bin/env'\n"
    printf "      or open a new terminal to use 'skyvern' in this shell.\n"
fi
