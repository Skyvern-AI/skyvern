#!/bin/bash

set -e

# setting api key
VITE_SKYVERN_API_KEY=$(sed -n 's/.*cred\s*=\s*"\([^"]*\)".*/\1/p' .streamlit/secrets.toml)
export VITE_SKYVERN_API_KEY
npm run start


