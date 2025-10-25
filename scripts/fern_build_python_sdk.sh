#!/usr/bin/env bash

CURRENT_VERSION=$(grep '^version = ' pyproject.toml | sed 's/version = "\(.*\)"/\1/')
fern generate --group python-sdk --log-level debug --version $CURRENT_VERSION --preview
cp -rf fern/.preview/fern-python-sdk/src/skyvern/* skyvern/client/