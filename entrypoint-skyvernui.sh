#!/bin/bash

set -e

# Extract API key from secrets file
VITE_SKYVERN_API_KEY=$(sed -n 's/.*cred\s*=\s*"\([^"]*\)".*/\1/p' .streamlit/secrets.toml 2>/dev/null || echo "")

# Inject API key into pre-built JS files (replace placeholder)
if [ -n "$VITE_SKYVERN_API_KEY" ]; then
    find /app/dist -name "*.js" -exec sed -i "s/__SKYVERN_API_KEY_PLACEHOLDER__/$VITE_SKYVERN_API_KEY/g" {} \;
fi

# Start the servers (no rebuild needed)
node localServer.js & node artifactServer.js & wait


