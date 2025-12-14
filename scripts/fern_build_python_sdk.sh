#!/usr/bin/env bash

CURRENT_VERSION=$(grep '^version = ' pyproject.toml | sed 's/version = "\(.*\)"/\1/')
fern generate --group python-sdk --log-level debug --version "$CURRENT_VERSION" --preview

rm -fr skyvern/client
mkdir -p skyvern/client
cp -rf fern/.preview/fern-python-sdk/src/skyvern/* skyvern/client/

# Post-processing: Patch version.py to handle missing metadata gracefully
VERSION_FILE="skyvern/client/version.py"
if [ -f "$VERSION_FILE" ]; then
    sed -i.bak 's/__version__ = metadata\.version("skyvern")/try:\
    __version__ = metadata.version("skyvern")\
except Exception:\
    __version__ = "0.0.0"/' "$VERSION_FILE"
    rm -f "${VERSION_FILE}.bak"
fi