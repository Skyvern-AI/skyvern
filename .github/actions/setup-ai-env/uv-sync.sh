#!/usr/bin/env bash
set -euo pipefail

uv_sync_args=(sync)
IFS=',' read -ra extra_array <<< "${UV_SYNC_EXTRAS:-}"
for extra in "${extra_array[@]}"; do
  trimmed=$(printf '%s' "$extra" | xargs)
  if [[ -n "$trimmed" ]]; then
    uv_sync_args+=(--extra "$trimmed")
  fi
done

IFS=',' read -ra group_array <<< "${UV_SYNC_GROUPS:-}"
for group in "${group_array[@]}"; do
  trimmed=$(printf '%s' "$group" | xargs)
  if [[ -n "$trimmed" ]]; then
    uv_sync_args+=(--group "$trimmed")
  fi
done

uv "${uv_sync_args[@]}"
