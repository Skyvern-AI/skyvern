#!/usr/bin/env bash

CURRENT_VERSION=$(grep '^version = ' pyproject.toml | sed 's/version = "\(.*\)"/\1/')
fern generate --group ts-sdk --log-level debug --version "$CURRENT_VERSION" --preview

mkdir -p skyvern-ts/client
mv skyvern-ts/client/src/library skyvern-ts/library
rm -rf skyvern-ts/client
mkdir -p skyvern-ts/client/src/library
mv skyvern-ts/library skyvern-ts/client/src/
cp -rf fern/.preview/fern-typescript-sdk/* skyvern-ts/client/

# Post-processing: Update repository references the monorepo
sed -i.bak 's|Skyvern-AI/skyvern-typescript|Skyvern-AI/skyvern|g' skyvern-ts/client/package.json
sed -i.bak 's|https://github.com/Skyvern-AI/skyvern-typescript/blob/HEAD/./reference.md|https://www.skyvern.com/docs/api-reference/api-reference|g' skyvern-ts/client/README.md
rm -f skyvern-ts/client/package.json.bak skyvern-ts/client/README.md.bak

# Export library classes from main index
cat >> skyvern-ts/client/src/index.ts << 'EOF'
export { Skyvern, SkyvernBrowser, SkyvernBrowserPageAgent, SkyvernBrowserPageAi } from "./library/index.js";
export type { SkyvernOptions, SkyvernBrowserPage } from "./library/index.js";
EOF

# Rename the API namespace to avoid conflict with Skyvern class
sed -i.bak 's/export \* as Skyvern from/export * as SkyvernApi from/g' skyvern-ts/client/src/index.ts
rm -f skyvern-ts/client/src/index.ts.bak

(cd skyvern-ts/client \
  && rm -rf node_modules package-lock.json \
  && npm install \
  && npx tsc --project ./tsconfig.cjs.json \
  && npx tsc --project ./tsconfig.esm.json \
  && node scripts/rename-to-esm-files.js dist/esm)

pre-commit run --all-files
