---
name: bump-version
description: Bump the Skyvern OSS version in every file that carries it and create the release PR. Use when releasing a new version or when the user asks to bump version.
argument-hint: [version]
disable-model-invocation: true
---

# Bump Version Skill

Automate the complete OSS version bump and release workflow for Skyvern.

## What this does


1. Validates and updates version in `pyproject.toml`
2. Sets the same version in the UI package, the TypeScript SDK and `uv.lock`
3. Creates commit with all changes
4. Optionally runs SDK tests
5. Pushes branch and creates PR

## Version argument

The version can be provided as an argument or you'll be prompted:

- If `$ARGUMENTS` is provided, use it as the new version
- If not provided, ask user for the new version number
- Validate it follows semver format: `MAJOR.MINOR.PATCH` (e.g., `1.0.14`, `1.1.0`, `2.0.0`)

**Semver guidance:**
- PATCH: Bug fixes, backwards compatible (e.g., 1.0.13 → 1.0.14)
- MINOR: New features, backwards compatible (e.g., 1.0.13 → 1.1.0)
- MAJOR: Breaking changes (e.g., 1.0.13 → 2.0.0)

## Step-by-step process

### 1. Get and validate version

- Read current version from `pyproject.toml` line 3
- Determine new version from `$ARGUMENTS` or prompt user
- Validate semver format using regex: `^\d+\.\d+\.\d+$`
- Confirm with user: "Bumping version from {current} to {new}. Continue?"

### 2. Create feature branch

```bash
git checkout -b bump-version-$ARGUMENTS
```

Branch naming: `bump-version-{version}` (e.g., `bump-version-1.0.14`)

### 3. Update pyproject.toml

Update line 3 in `pyproject.toml`:

```toml
version = "{new_version}"
```

Use the Edit tool to make this single-line change.

### 4. Update the other version files

The same version lives in four more files. Set each to `{new_version}` with the Edit tool:

- `packages/skyvern-ui/pyproject.toml`: `version` on line 3
- `skyvern-ts/client/package.json`: the `version` field
- `skyvern-ts/client/package-lock.json`: the top-level `version`, and the `version` under `packages[""]`
- `uv.lock`: the `version` in the `[[package]]` entry whose `name` is `"skyvern"`

The SDK clients are ordinary source. A release changes their version only; nothing is regenerated.

### 5. Review changes

```bash
git status
git diff --stat
```

Expect exactly five files, one or two lines each. Show user:
- Number of files changed
- Which files were modified
- Summary of changes

### 6. Commit changes

```bash
git add pyproject.toml packages/skyvern-ui/pyproject.toml uv.lock skyvern-ts/client/package.json skyvern-ts/client/package-lock.json
git commit -m "Bump version to {version}

- Update version in pyproject.toml, packages/skyvern-ui/pyproject.toml and uv.lock
- Update version in skyvern-ts/client/package.json and package-lock.json

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

### 7. Verify SDKs (optional)

Ask user: "Would you like to run SDK tests to verify nothing broke?"

If yes:
- Python SDK tests: `pytest tests/sdk/python_sdk/`
- Note that TypeScript tests require manual Chrome setup (see `tests/sdk/README.md`)
- Display test results
- If tests fail, STOP and report errors - do not proceed to push

If no:
- Skip to push step

### 8. Push and create PR

Ask user: "Ready to push and create PR?"

If yes:

```bash
git push -u origin bump-version-{version}

gh pr create --title "Bump version to {version}" --body "## Summary
Bump Skyvern OSS version to {version}

## Changes
- Updated version in \`pyproject.toml\`, \`packages/skyvern-ui/pyproject.toml\` and \`uv.lock\`
- Updated version in \`skyvern-ts/client/package.json\` and \`package-lock.json\`

## Deployment
After merge, GitHub will automatically:
- Deploy Python package to PyPI (version change in \`pyproject.toml\`)
- Deploy TypeScript package to NPM (version change in \`package.json\`)

## Testing
- [ ] Python SDK tests passed locally
- [ ] TypeScript SDK tests passed locally (if applicable)

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

Display the PR URL to the user.

## Important notes

- **Single PR**: All changes (the five version edits and the commit) happen in one PR
- **Five files**: the version is set by hand in each; no tool copies it from `pyproject.toml`
- **Testing**: SDK tests require `.env` with `SKYVERN_API_KEY`
- **Deployment**: Automatic on PR merge via GitHub Actions
- **No force push**: Never use `--force` when pushing

## Error handling

If any step fails:
1. Display the error message clearly
2. Explain what went wrong
3. Ask user how to proceed (fix, skip, or abort)
4. Do not continue to next steps if critical operations fail
