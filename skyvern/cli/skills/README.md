# Skyvern Skills Package

AI-powered browser automation skill for coding agents. Bundled with `pip install skyvern`.

## Quick Start

```bash
pip install skyvern
export SKYVERN_API_KEY="YOUR_KEY"   # get one at https://app.skyvern.com
```

The skill teaches CLI commands via `skyvern <command>` invocations. For richer
AI-coding-tool integration, run `skyvern setup claude-code --project` to enable
MCP (Model Context Protocol) with auto-tool-calling.

## What's Included

A single `skyvern` skill covering all browser automation capabilities:

- Browser session lifecycle (create, navigate, close)
- AI actions: act, extract, validate, screenshot
- Precision primitives: click, type, hover, scroll, select, press-key, wait
- One-off tasks with run-task
- Credential management and secure login flows
- Workflow CRUD, execution, monitoring, and cancellation
- Block schema discovery and validation
- Debugging with screenshot + validate loops

## Structure

```
skyvern/
  SKILL.md              Main skill file (CLI-first, all capabilities)
  references/           17 deep-dive reference files
  examples/             Workflow JSON examples
```

## Install to a Project

```bash
# Copy skill files to your project
skyvern skill copy --output .claude/skills
skyvern skill copy --output .agents/skills
```

## Validate

```bash
python scripts/validate_skills_package.py
```
