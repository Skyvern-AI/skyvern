# Skyvern Skills Package

AI-powered browser automation skills for coding agents. Bundled with `pip install skyvern`.

## Quick Start

```bash
pip install skyvern
export SKYVERN_API_KEY="YOUR_KEY"   # get one at https://app.skyvern.com

# Set up MCP + install skills in one step:
skyvern setup claude-code
```

`skyvern setup claude-code` registers the Skyvern MCP server and installs these
skills into your project's `.claude/skills/` directory automatically.

## What's Included

### qa
QA test your frontend changes in a real browser. Reads your `git diff`, generates
targeted browser tests, runs them against your local dev server, and reports
pass/fail with screenshots. Invoke with `/qa` in Claude Code.

### skyvern
CLI reference covering all browser automation capabilities:

- Browser session lifecycle (create, navigate, close)
- AI actions: act, extract, validate, screenshot
- Precision primitives: click, type, hover, scroll, select, press-key, wait
- One-off tasks with run-task
- Credential management and secure login flows
- Workflow CRUD, execution, monitoring, and cancellation
- Block schema discovery and validation
- Debugging with screenshot + validate loops

### testing
Smoke-test skill for verifying Skyvern deployments.

## Structure

```
qa/
  SKILL.md              Diff-driven frontend QA testing
skyvern/
  SKILL.md              Main skill file (CLI-first, all capabilities)
  references/           17 deep-dive reference files
  examples/             Workflow JSON examples
testing/
  SKILL.md              Deployment smoke testing
```

## Manual Install

If you prefer to install skills without running setup:

```bash
skyvern skill copy --output .claude/skills
skyvern skill copy --output .agents/skills
```

## Validate

```bash
python scripts/validate_skills_package.py
```
