#!/usr/bin/env bash

CURRENT_VERSION=$(grep '^version = ' pyproject.toml | sed 's/version = "\(.*\)"/\1/')
fern generate --group ts-sdk --log-level debug --version "$CURRENT_VERSION" --preview

(cd fern/.preview/fern-typescript-sdk \
  && npm install \
  && npx tsc --project ./tsconfig.cjs.json \
  && npx tsc --project ./tsconfig.esm.json \
  && node scripts/rename-to-esm-files.js dist/esm)

rm -fr skyvern-ts/client
mkdir -p skyvern-ts/client
cp -rf fern/.preview/fern-typescript-sdk/* skyvern-ts/client/

# Post-processing: Update repository references the monorepo
sed -i.bak 's|Skyvern-AI/skyvern-typescript|Skyvern-AI/skyvern|g' skyvern-ts/client/package.json
sed -i.bak 's|https://github.com/Skyvern-AI/skyvern-typescript/blob/HEAD/./reference.md|https://www.skyvern.com/docs/api-reference/api-reference|g' skyvern-ts/client/README.md
rm -f skyvern-ts/client/package.json.bak skyvern-ts/client/README.md.bak