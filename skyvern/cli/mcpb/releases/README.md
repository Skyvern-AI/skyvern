# Public Claude Desktop Bundle

This directory holds the committed `skyvern-claude-desktop.mcpb` artifact that
syncs to the public `Skyvern-AI/skyvern` repository.

The public Fern docs and `skyvern setup claude` link directly to the raw file in
that OSS repo so users can click once and download the installer immediately.

Refresh it — set `<version>` to one higher than the last release (the current
version is stamped in `manifest.json` inside the committed bundle):

```bash
./scripts/package-mcpb.sh <version> skyvern-claude-desktop.mcpb \
  skyvern/cli/mcpb/releases/skyvern-claude-desktop.mcpb
```

The build vendors the full runtime dependency tree into the bundle, so audit for
vulnerable transitive dependencies before publishing:

```bash
cd skyvern/cli/mcpb/claude_desktop && npm audit --omit=dev
```

Resolve any HIGH/CRITICAL findings (pin via an `overrides` block in
`package.json`, then `npm install`) and rebuild before shipping.
