# Skyvern Claude Desktop MCP Bundle

Source files for the downloadable `.mcpb` bundle that installs Skyvern Cloud into Claude Desktop without requiring the user to install Node.js.

Build locally with (`<version>` is any semver string, stamped into the bundle's
manifest — for a real release, bump one higher than the last published version):

```bash
./scripts/package-mcpb.sh <version>
```

To refresh the stable public download that syncs to the OSS repo:

```bash
./scripts/package-mcpb.sh <version> skyvern-claude-desktop.mcpb \
  skyvern/cli/mcpb/releases/skyvern-claude-desktop.mcpb
```

Cloud maintainers (skyvern-cloud repo only): the full release + security-audit +
publish-chain runbook lives at `cloud_docs/mcp/CLAUDE_DESKTOP_BUNDLE_RELEASE.md`.
