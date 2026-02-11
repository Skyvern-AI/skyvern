#!/bin/bash

set -e

# Set ALLOWED_SKIP_DB_MIGRATION_VERSION env var to the DB version you want to allow (select * from alembic_version)
# If current DB matches this version, migrations will be skipped. Use at your own risk.
ALLOWED_SKIP_DB_MIGRATION_VERSION=${ALLOWED_SKIP_DB_MIGRATION_VERSION:-}

# Run migrations by default
run_migration=true

if [ -n "$ALLOWED_SKIP_DB_MIGRATION_VERSION" ]; then
    current_version=$(alembic current 2>&1 | grep -Eo "[0-9a-f]{12,}" | tail -n 1 || echo "")
    echo "Current DB version: $current_version"

    if [ "$current_version" = "$ALLOWED_SKIP_DB_MIGRATION_VERSION" ]; then
        echo "⚠️  WARNING: Skipping database migrations"
        echo "⚠️  DB is at version $current_version which matches ALLOWED_SKIP_DB_MIGRATION_VERSION"
        echo "⚠️  Running older code against newer database schema"
        echo "⚠️  Beware of compatibility risks!"
        run_migration=false
    else
        echo "Current DB version ($current_version) does not match ALLOWED_SKIP_DB_MIGRATION_VERSION ($ALLOWED_SKIP_DB_MIGRATION_VERSION)"
    fi
fi

if [ "$run_migration" = true ]; then
    echo "Running database migrations..."
    alembic upgrade head
    alembic check
fi

# Create org and API key if not present (write to shared dir for UI, export for backend)
API_KEY_FILE="${API_KEY_FILE:-/app/data/api_key.txt}"
mkdir -p "$(dirname "$API_KEY_FILE")"
if [ ! -f "$API_KEY_FILE" ]; then
    echo "Creating organization and API token..."
    org_output=$(python scripts/create_organization.py Testcharmvision-Open-Source)
    api_token=$(echo "$org_output" | awk '/token=/{gsub(/.*token='\''|'\''.*/, ""); print}')
    echo "$api_token" > "$API_KEY_FILE"
    export TESTCHARMVISION_API_KEY="$api_token"
    echo "API key written to $API_KEY_FILE"
else
    export TESTCHARMVISION_API_KEY=$(cat "$API_KEY_FILE")
fi

_kill_xvfb_on_term() {
  kill -TERM $xvfb
}

# Setup a trap to catch SIGTERM and relay it to child processes
trap _kill_xvfb_on_term TERM

echo "Starting Xvfb..."
# delete the lock file if any
rm -f /tmp/.X99-lock
# Set display environment variable
export DISPLAY=:99
# Start Xvfb
Xvfb :99 -screen 0 1920x1080x16 &
xvfb=$!

DISPLAY=:99 xterm 2>/dev/null &

# Run the command and pass in all three arguments
python -m testcharmvision.forge
