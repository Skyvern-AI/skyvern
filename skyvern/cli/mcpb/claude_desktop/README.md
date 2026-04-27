# Skyvern Claude Desktop MCP Bundle

Source files for the downloadable `.mcpb` bundle that installs Skyvern Cloud into Claude Desktop without requiring the user to install Node.js.

Build locally with:

```bash
./scripts/package-mcpb.sh 1.0.23
```

To refresh the stable public download that syncs to the OSS repo:

```bash
./scripts/package-mcpb.sh 1.0.23 skyvern-claude-desktop.mcpb \
  skyvern/cli/mcpb/releases/skyvern-claude-desktop.mcpb
```
