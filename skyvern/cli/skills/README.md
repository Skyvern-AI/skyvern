# Skyvern Skills Package

AI-powered browser automation skills for coding agents. Bundled with `pip install skyvern`.

## Quick Start

```bash
pip install skyvern

# Recommended local self-hosted path:
skyvern quickstart   # or: skyvern init
# choose local
# choose Claude Code during the MCP step

# You can also configure Claude Code later:
skyvern setup claude-code
```

The local wizard path writes project-local `.mcp.json`, pins the MCP command to
your active Python interpreter, and installs these skills into
`.claude/skills/` automatically. `skyvern setup claude-code` does the same
setup later if you skipped it during `quickstart` / `init`.

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

### smoke-test
CI-oriented smoke testing via Skyvern browser tools. Reads your `git diff`, generates
3-8 targeted smoke tests, runs each one via Skyvern browser tools (navigate, act,
validate, screenshot), and reports a pass/fail table as a PR comment. Invoke with
`/smoke-test` in Claude Code.

### testing
Deployment health check for verifying Skyvern installations.

## Structure

```
qa/
  SKILL.md              Diff-driven frontend QA testing
smoke-test/
  SKILL.md              CI-oriented smoke testing via Skyvern browser tools
skyvern/
  SKILL.md              Main skill file (CLI-first, all capabilities)
  references/           17 deep-dive reference files
  examples/             Workflow JSON examples
testing/
  SKILL.md              Deployment health checking
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
