#!/usr/bin/env bash

CURRENT_VERSION=$(grep '^version = ' pyproject.toml | sed 's/version = "\(.*\)"/\1/')
fern generate --group ts-sdk --log-level debug --version $CURRENT_VERSION --preview

(cd fern/.preview/fern-typescript-sdk \
  && npm install \
  && npx tsc --project ./tsconfig.cjs.json \
  && npx tsc --project ./tsconfig.esm.json \
  && node scripts/rename-to-esm-files.js dist/esm)

cp -rf fern/.preview/fern-typescript-sdk/* skyvern-ts/client/