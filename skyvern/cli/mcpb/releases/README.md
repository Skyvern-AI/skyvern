# Public Claude Desktop Bundle

This directory holds the committed `skyvern-claude-desktop.mcpb` artifact that
syncs to the public `Skyvern-AI/skyvern` repository.

The public Fern docs and `skyvern setup claude` link directly to the raw file in
that OSS repo so users can click once and download the installer immediately.

Refresh it with:

```bash
./scripts/package-mcpb.sh 1.0.23 skyvern-claude-desktop.mcpb \
  skyvern/cli/mcpb/releases/skyvern-claude-desktop.mcpb
```
