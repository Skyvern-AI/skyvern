#!/bin/bash

set -e

# Default values for environment variables
VITE_API_BASE_URL="${VITE_API_BASE_URL:-http://localhost:8000/api/v1}"
VITE_WSS_BASE_URL="${VITE_WSS_BASE_URL:-ws://localhost:8000/api/v1}"
VITE_ARTIFACT_API_BASE_URL="${VITE_ARTIFACT_API_BASE_URL:-http://localhost:9090}"

# Priority for VITE_SKYVERN_API_KEY:
# 1. Environment variable (from .env file or docker-compose environment)
# 2. Fallback to .streamlit/secrets.toml
if [ -n "$VITE_SKYVERN_API_KEY" ]; then
    # Trim whitespace and validate
    VITE_SKYVERN_API_KEY=$(echo "$VITE_SKYVERN_API_KEY" | xargs)
    echo "Using VITE_SKYVERN_API_KEY from environment variable"
else
    # Fallback: Extract API key from secrets file
    VITE_SKYVERN_API_KEY=$(sed -n 's/.*cred\s*=\s*"\([^"]*\)".*/\1/p' .streamlit/secrets.toml 2>/dev/null || echo "")
    if [ -n "$VITE_SKYVERN_API_KEY" ]; then
        echo "Using VITE_SKYVERN_API_KEY from .streamlit/secrets.toml"
    else
        echo "WARNING: No VITE_SKYVERN_API_KEY found in environment or .streamlit/secrets.toml"
    fi
fi

# Inject environment variables into pre-built JS files (replace placeholders)
# Using | as delimiter since URLs contain /
find /app/dist -name "*.js" -exec sed -i \
    -e "s|__VITE_API_BASE_URL_PLACEHOLDER__|${VITE_API_BASE_URL}|g" \
    -e "s|__VITE_WSS_BASE_URL_PLACEHOLDER__|${VITE_WSS_BASE_URL}|g" \
    -e "s|__VITE_ARTIFACT_API_BASE_URL_PLACEHOLDER__|${VITE_ARTIFACT_API_BASE_URL}|g" \
    -e "s|__SKYVERN_API_KEY_PLACEHOLDER__|${VITE_SKYVERN_API_KEY}|g" \
    {} \;

# Start the servers (no rebuild needed)
# Tini (configured as ENTRYPOINT) handles signal forwarding and zombie reaping
node localServer.js &
node artifactServer.js &
wait

