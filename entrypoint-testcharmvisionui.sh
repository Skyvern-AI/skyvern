#!/bin/bash

set -e

# API key from shared file (written by backend entrypoint) or env
if [ -f "${API_KEY_FILE:-/app/data/api_key.txt}" ]; then
  export VITE_TESTCHARMVISION_API_KEY=$(cat "${API_KEY_FILE:-/app/data/api_key.txt}")
fi
npm run start


