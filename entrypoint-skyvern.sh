#!/bin/bash

set -e

# ---------------------------------------------------------------------------
# Ensure the target database exists (POSTGRES_DB is only honoured on first
# volume init — a stale postgres-data dir means the DB may be missing).
# ---------------------------------------------------------------------------
if [ -n "$DATABASE_STRING" ]; then
    # Parse postgresql+psycopg://user:pass@host:port/dbname
    db_name=$(echo "$DATABASE_STRING" | sed -n 's|.*/.*/\([^?]*\).*|\1|p')
    db_user=$(echo "$DATABASE_STRING" | sed -n 's|.*://\([^:]*\):.*|\1|p')
    db_host=$(echo "$DATABASE_STRING" | sed -n 's|.*@\([^:]*\):.*|\1|p')
    db_port=$(echo "$DATABASE_STRING" | sed -n 's|.*:\([0-9]*\)/.*|\1|p')

    if [ -n "$db_name" ] && [ -n "$db_user" ] && [ -n "$db_host" ]; then
        export PGHOST="$db_host"
        export PGPORT="${db_port:-5432}"
        export PGUSER="$db_user"
        # Extract password (between first : after :// and @)
        db_pass=$(echo "$DATABASE_STRING" | sed -n 's|.*://[^:]*:\([^@]*\)@.*|\1|p')
        if [ -n "$db_pass" ]; then
            export PGPASSWORD="$db_pass"
        fi

        if psql -d "$db_name" -c "SELECT 1" > /dev/null 2>&1; then
            echo "✅ Database '$db_name' exists."
        else
            echo "Database '$db_name' not found — creating..."
            createdb "$db_name" && echo "✅ Database '$db_name' created." \
                || echo "⚠️  Could not create database '$db_name' — migrations may fail."
        fi
        unset PGPASSWORD
    fi
fi

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

SKYVERN_CREDENTIALS_FILE="${SKYVERN_CREDENTIALS_FILE:-/app/.skyvern/credentials.toml}"
mkdir -p "$(dirname "$SKYVERN_CREDENTIALS_FILE")"

if [ ! -f "$SKYVERN_CREDENTIALS_FILE" ]; then
    echo "Creating organization and API token..."
    org_output=$(python scripts/create_organization.py Skyvern-Open-Source)
    api_token=$(echo "$org_output" | awk '/token=/{gsub(/.*token='\''|'\''.*/, ""); print}')
    echo -e "[skyvern]\nconfigs = [\n    {\"env\" = \"local\", \"host\" = \"http://skyvern:8000/api/v1\", \"orgs\" = [{name=\"Skyvern\", cred=\"$api_token\"}]}\n]" > "$SKYVERN_CREDENTIALS_FILE"
    echo "$SKYVERN_CREDENTIALS_FILE file updated with organization details."
fi

_kill_xvfb_on_term() {
  kill -TERM $xvfb
}

# Setup a trap to catch SIGTERM and relay it to child processes
trap _kill_xvfb_on_term TERM

echo "Starting Xvfb..."
display_number="${SKYVERN_DEFAULT_DISPLAY:-99}"
if [[ ! "$display_number" =~ ^[0-9]+$ ]]; then
  printf 'ERROR: SKYVERN_DEFAULT_DISPLAY must be an unsigned integer; got %q\n' "$display_number" >&2
  exit 1
fi
display_address=":${display_number}"
# delete the lock file if any
rm -f "/tmp/.X${display_number}-lock"
# Set display environment variable
export DISPLAY="$display_address"
# Start Xvfb
Xvfb "$display_address" -screen 0 1920x1080x16 &
xvfb=$!

DISPLAY="$display_address" xterm 2>/dev/null &

# Wait for Xvfb to be ready before starting x11vnc
for i in $(seq 1 10); do
  xdpyinfo -display "$display_address" >/dev/null 2>&1 && break
  echo "Waiting for Xvfb to start (attempt $i/10)..."
  sleep 1
done
if ! xdpyinfo -display "$display_address" >/dev/null 2>&1; then
  echo "ERROR: Xvfb failed to start on display $display_address after 10 attempts"
  exit 1
fi

if [ "${BROWSER_STREAMING_MODE:-}" != "vnc" ]; then
  echo "Starting x11vnc on display $display_address..."
  # VNC runs without a password (-nopw) because port 5900 is not exposed outside
  # the container. Browser streaming reaches users via websockify on port 6080.
  x11vnc_log_dir="${LOG_PATH:-/data/log}"
  mkdir -p "$x11vnc_log_dir"
  x11vnc -display "$display_address" -forever -nopw -shared -rfbport 5900 -bg -o /dev/null \
    2>"$x11vnc_log_dir/x11vnc.err"

  echo "Starting websockify on port 6080 -> localhost:5900..."
  websockify 6080 localhost:5900 --daemon
else
  echo "Dynamic VNC mode enabled; per-session VNC ports are started on demand."
fi

python run_streaming.py > /dev/null &

# Run the command and pass in all three arguments
python -m skyvern.forge
