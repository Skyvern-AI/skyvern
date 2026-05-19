#!/usr/bin/env bash
# Mirror the package-shipped Skyvern skill (canonical, used by `skyvern skill` CLI)
# to the repo-root `skills/` collection (required by Codex Marketplace submissions).
#
# The package copy is the source of truth because `skyvern/cli/skill_commands.py`
# resolves skills via `Path(__file__).parent / "skills"` and the wheel only ships
# files under `skyvern/`. The repo-root copy exists solely so codex-marketplace.com
# can discover the skill at the required `skills/<name>/` path.
#
# Run this whenever the package skill is edited, then commit the mirror.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO_ROOT/skyvern/cli/skills"
DST="$REPO_ROOT/skills"

# Skills intended for marketplace publication. Add more names here as they're created.
SKILLS_TO_MIRROR=(skyvern)

mkdir -p "$DST"
for skill in "${SKILLS_TO_MIRROR[@]}"; do
  if [[ -d "$SRC/$skill" ]]; then
    rm -rf "${DST:?}/${skill:?}"
    cp -r "$SRC/$skill" "$DST/$skill"
    echo "synced: $SRC/$skill -> $DST/$skill"
  fi
done
