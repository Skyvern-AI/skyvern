#!/usr/bin/env bash
# Detect direct calls to AgentDB backward-compatible delegate methods.
# New code must use repository attributes (e.g. db.tasks.create_task)
# instead of the legacy delegates (e.g. db.create_task).
#
# Called by tests/unit/test_no_direct_db_delegates.py

set -euo pipefail

AGENT_DB="skyvern/forge/sdk/db/agent_db.py"

# Extract delegate method names from agent_db.py.
# These are the "async def <name>" lines inside the delegate section (after line 170).
delegate_methods=$(
    awk 'NR > 170 && /^    async def / { gsub(/.*async def /,""); gsub(/\(.*/,""); print }' "$AGENT_DB" \
    | sort -u
)

if [ -z "$delegate_methods" ]; then
    echo "ERROR: Could not extract delegate methods from $AGENT_DB" >&2
    exit 1
fi

# Build a grep alternation pattern for delegate method names.
methods_pattern=$(echo "$delegate_methods" | paste -sd'|' -)

# Search for direct delegate calls on known AgentDB access patterns:
#   app.DATABASE.<method>(     — should be app.DATABASE.<repo>.<method>(
#   REPLICA_DATABASE.<method>( — should be REPLICA_DATABASE.<repo>.<method>(
# Exclude the delegate file itself and tests.
db_pattern="(DATABASE|REPLICA_DATABASE)\.(${methods_pattern})\("

# Legacy files that still use direct delegates (grandfathered in).
ALLOWLIST=(
    "$AGENT_DB"
    "tests/"
    "run_streaming.py"
)

exclude_args=()
for allowed in "${ALLOWLIST[@]}"; do
    exclude_args+=(":!${allowed}")
done

violations=$(
    git grep -n -E "$db_pattern" -- '*.py' "${exclude_args[@]}" \
        2>/dev/null \
    || true
)

if [ -n "$violations" ]; then
    echo "Direct AgentDB delegate calls found. Use repository attributes instead."
    echo "  e.g. app.DATABASE.tasks.create_task(...) not app.DATABASE.create_task(...)"
    echo ""
    echo "$violations"
    exit 1
fi

echo "No direct delegate calls found."
exit 0
