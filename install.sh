#!/bin/sh
# Skyvern installer. Run `install.sh --help` for usage.

set -eu

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
            # Heredoc rather than self-parsing $0: when piped, $0 is the
            # shell name, not a real file path.
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
  1. Bootstraps 'uv' if missing — uv manages Python so the host
     doesn't need one preinstalled.
  2. Installs the variant's PyPI package as an isolated tool
     ('skyvern' on PATH).
  3. For variants that need a browser, runs 'skyvern init' to fetch
     Chromium (~150MB; skip with --no-init).
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
    if ! curl -LsSf https://astral.sh/uv/install.sh -o "$UV_INSTALLER"; then
        rm -f "$UV_INSTALLER"
        printf 'ERROR: failed to download uv installer from astral.sh.\n' >&2
        printf '       Check your network/proxy. See https://docs.astral.sh/uv/getting-started/installation/\n' >&2
        exit 1
    fi
    sh "$UV_INSTALLER"
    rm -f "$UV_INSTALLER"
    export PATH="$HOME/.local/bin:$PATH"
    if ! command -v uv >/dev/null 2>&1; then
        printf 'ERROR: uv installer ran but uv is not on PATH.\n' >&2
        printf '       See https://docs.astral.sh/uv/getting-started/installation/\n' >&2
        exit 1
    fi
fi

# Variant policy: each variant sets the PyPI spec, the Python range to pin,
# whether 'skyvern init' runs by default, and the success-message hint.
# Today only 'server' is accepted; new variants get added at SDK split time.
case "$SKYVERN_VARIANT" in
    server)
        PKG_SPEC="skyvern"
        PYTHON_SPEC=">=3.11,<3.14"
        DEFAULT_RUN_INIT=1
        SUCCESS_HINT="Try: skyvern --help"
        ;;
    sdk|mcp|all)
        printf "ERROR: --variant '%s' is reserved for the upcoming SDK package split\n" "$SKYVERN_VARIANT" >&2
        printf "       and is not yet available. Use --variant server (the default) for now.\n" >&2
        exit 2
        ;;
    *)
        printf "ERROR: unknown variant '%s'. Valid: server (default).\n" "$SKYVERN_VARIANT" >&2
        exit 2
        ;;
esac

[ -n "$SKYVERN_RUN_INIT" ] || SKYVERN_RUN_INIT="$DEFAULT_RUN_INIT"

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

if [ "$SKYVERN_RUN_INIT" = "1" ]; then
    printf "\nrunning 'skyvern init' (installs Chromium, ~150MB)...\n"
    if ! skyvern init; then
        # Chromium download is best-effort: corp proxies often block it. The
        # CLI is installed regardless; surface a recovery hint and continue.
        printf "\nWARN: 'skyvern init' did not complete. The CLI is installed;\n" >&2
        printf "      re-run 'skyvern init' once your network/proxy is sorted.\n" >&2
    fi
fi

printf '\nSkyvern installed. %s\n' "$SUCCESS_HINT"

if [ "$uv_was_freshly_installed" = "1" ]; then
    printf "\nNote: uv was just installed in ~/.local/bin. Run 'source ~/.local/bin/env'\n"
    printf "      or open a new terminal to use 'skyvern' in this shell.\n"
fi
