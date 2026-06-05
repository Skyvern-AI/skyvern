#!/bin/bash

set -euo pipefail

# Escape special sed characters in a value so it can be used safely in a
# sed replacement string (s|old|new|g). Handles |, &, \, and / which are
# metacharacters. Only handles single-line values (sufficient for URLs,
# API keys, booleans, and path prefixes used here); a multi-line value
# would corrupt the bundle silently, so fail fast instead.
escape_sed() {
    if [[ "$1" == *$'\n'* ]]; then
        echo "ERROR: escape_sed called with multi-line value; refusing to corrupt the bundle" >&2
        exit 1
    fi
    printf '%s\n' "$1" | sed -e 's/[&/\|]/\\&/g'
}

# Default values for environment variables
VITE_API_BASE_URL="${VITE_API_BASE_URL:-http://localhost:8000/api/v1}"
VITE_WSS_BASE_URL="${VITE_WSS_BASE_URL:-ws://localhost:8000/api/v1}"
VITE_ARTIFACT_API_BASE_URL="${VITE_ARTIFACT_API_BASE_URL:-http://localhost:9090}"
VITE_BROWSER_STREAMING_MODE="${VITE_BROWSER_STREAMING_MODE:-vnc}"
VITE_ENABLE_LOG_ARTIFACTS="${VITE_ENABLE_LOG_ARTIFACTS:-false}"
VITE_ENABLE_CODE_BLOCK="${VITE_ENABLE_CODE_BLOCK:-false}"
VITE_ENABLE_2FA_NOTIFICATIONS="${VITE_ENABLE_2FA_NOTIFICATIONS:-false}"

# Priority for VITE_SKYVERN_API_KEY:
# 1. Environment variable (from .env file or docker-compose environment),
#    but only if it's not a placeholder/sentinel value. Both the Dockerfile
#    default (__SKYVERN_API_KEY_PLACEHOLDER__) and the .env.example default
#    (YOUR_API_KEY) can leak through if users skip configuration. Keep this
#    filter list aligned with the frontend's PLACEHOLDER_VALUES.
# 2. Generated credentials file from the backend first-run setup
#    (volume-mounted at /app/.skyvern/credentials.toml in docker-compose)
VITE_SKYVERN_API_KEY="${VITE_SKYVERN_API_KEY:-}"
SKYVERN_CREDENTIALS_FILE="${SKYVERN_CREDENTIALS_FILE:-/app/.skyvern/credentials.toml}"
GENERATED_KEY=$(sed -n 's/.*cred\s*=\s*"\([^"]*\)".*/\1/p' "$SKYVERN_CREDENTIALS_FILE" 2>/dev/null || echo "")
if [ -n "$VITE_SKYVERN_API_KEY" ] \
    && [ "$VITE_SKYVERN_API_KEY" != "__SKYVERN_API_KEY_PLACEHOLDER__" ] \
    && [ "$VITE_SKYVERN_API_KEY" != "YOUR_API_KEY" ]; then
    VITE_SKYVERN_API_KEY="${VITE_SKYVERN_API_KEY#"${VITE_SKYVERN_API_KEY%%[![:space:]]*}"}"
    VITE_SKYVERN_API_KEY="${VITE_SKYVERN_API_KEY%"${VITE_SKYVERN_API_KEY##*[![:space:]]}"}"
    echo "Using VITE_SKYVERN_API_KEY from environment variable"
elif [ -n "$GENERATED_KEY" ]; then
    VITE_SKYVERN_API_KEY="$GENERATED_KEY"
    echo "Using VITE_SKYVERN_API_KEY from $SKYVERN_CREDENTIALS_FILE"
else
    echo "WARNING: No VITE_SKYVERN_API_KEY found in environment or $SKYVERN_CREDENTIALS_FILE"
    VITE_SKYVERN_API_KEY=""
fi

echo "Injecting runtime environment variables into pre-built assets..."

# Rebuild from the pristine template each start so sed sees fresh placeholders
# and `docker restart` re-applies current env values (e.g. rotated API key).
rm -rf /app/dist
cp -r /app/dist.template /app/dist

# Escape all values for safe use in sed replacement patterns
ESC_API_BASE_URL=$(escape_sed "$VITE_API_BASE_URL")
ESC_WSS_BASE_URL=$(escape_sed "$VITE_WSS_BASE_URL")
ESC_ARTIFACT_API_BASE_URL=$(escape_sed "$VITE_ARTIFACT_API_BASE_URL")
ESC_SKYVERN_API_KEY=$(escape_sed "$VITE_SKYVERN_API_KEY")
ESC_BROWSER_STREAMING_MODE=$(escape_sed "$VITE_BROWSER_STREAMING_MODE")
ESC_ENABLE_LOG_ARTIFACTS=$(escape_sed "$VITE_ENABLE_LOG_ARTIFACTS")
ESC_ENABLE_CODE_BLOCK=$(escape_sed "$VITE_ENABLE_CODE_BLOCK")
ESC_ENABLE_2FA_NOTIFICATIONS=$(escape_sed "$VITE_ENABLE_2FA_NOTIFICATIONS")

# Replace placeholder strings in pre-built JS and HTML files with actual runtime values
find /app/dist \( -name "*.js" -o -name "*.html" \) -exec sed -i \
    -e "s|__VITE_API_BASE_URL_PLACEHOLDER__|${ESC_API_BASE_URL}|g" \
    -e "s|__VITE_WSS_BASE_URL_PLACEHOLDER__|${ESC_WSS_BASE_URL}|g" \
    -e "s|__VITE_ARTIFACT_API_BASE_URL_PLACEHOLDER__|${ESC_ARTIFACT_API_BASE_URL}|g" \
    -e "s|__SKYVERN_API_KEY_PLACEHOLDER__|${ESC_SKYVERN_API_KEY}|g" \
    -e "s|__VITE_BROWSER_STREAMING_MODE_PLACEHOLDER__|${ESC_BROWSER_STREAMING_MODE}|g" \
    -e "s|__VITE_ENABLE_LOG_ARTIFACTS_PLACEHOLDER__|${ESC_ENABLE_LOG_ARTIFACTS}|g" \
    -e "s|__VITE_ENABLE_CODE_BLOCK_PLACEHOLDER__|${ESC_ENABLE_CODE_BLOCK}|g" \
    -e "s|__VITE_ENABLE_2FA_NOTIFICATIONS_PLACEHOLDER__|${ESC_ENABLE_2FA_NOTIFICATIONS}|g" \
    {} +

# Sanity check: ensure ALL placeholders were actually replaced.
# If Vite ever splits/mangles a placeholder string, sed will silently
# succeed while leaving the placeholder in the bundle — catch that here.
# Scoped to *.js and *.html (the files sed modifies above) so unmodified
# sourcemaps don't trigger false positives.
if grep -rqE --include='*.js' --include='*.html' \
    '__VITE_[A-Z0-9_]+_PLACEHOLDER__|__SKYVERN_API_KEY_PLACEHOLDER__' /app/dist/; then
    echo "ERROR: Placeholder replacement incomplete in dist/. Unreplaced placeholders:" >&2
    grep -rhoE --include='*.js' --include='*.html' \
        '__VITE_[A-Z0-9_]+_PLACEHOLDER__|__SKYVERN_API_KEY_PLACEHOLDER__' /app/dist/ \
        | sort -u | sed 's/^/  /' >&2
    exit 1
fi

echo "Starting servers..."

# Ensure both servers are cleaned up if either one exits unexpectedly
trap 'kill $(jobs -p) 2>/dev/null; wait' EXIT

# Start the local server (serves pre-built static assets) and artifact server
node localServer.js &
node artifactServer.js &

# Exit as soon as either server terminates so the container restarts promptly.
# `wait -n` requires bash 4.3+; the base image (node:24.14-slim / Debian 12)
# ships bash 5.2 — revisit if the base image is ever swapped for one with an
# older bash (e.g. alpine's ash, which doesn't support `wait -n` at all).
wait -n
exit $?
