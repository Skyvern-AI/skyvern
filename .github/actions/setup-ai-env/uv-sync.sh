#!/usr/bin/env bash
set -euo pipefail

# Build uv sync command with optional --group flags from UV_SYNC_GROUPS env var.
# UV_SYNC_GROUPS: comma-separated list of groups (whitespace trimmed, empty entries ignored).

args=(sync)

if [[ -n "${UV_SYNC_GROUPS:-}" ]]; then
    IFS=',' read -ra raw_groups <<< "$UV_SYNC_GROUPS"
    for group in "${raw_groups[@]}"; do
        # Trim leading/trailing whitespace
        group="${group#"${group%%[![:space:]]*}"}"
        group="${group%"${group##*[![:space:]]}"}"
        if [[ -n "$group" ]]; then
            args+=(--group "$group")
        fi
    done
fi

uv "${args[@]}"
