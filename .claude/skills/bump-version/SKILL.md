---
name: bump-version
description: Release a new version of Skyvern. The version bump is made in the cloud repo and arrives here through the sync; this repo's only step is the skyvern-ui package. Use when the user asks to bump the version.
argument-hint: [version]
disable-model-invocation: true
---

# Bump Version Skill

A release is a version change on `main` of this repo. These workflows publish it:

| Workflow | Fires on a change to | Does |
|----------|----------------------|------|
| `sdk-release.yml` | `pyproject.toml` | Publishes `skyvern` and `skyvern-ui` to PyPI. Fails if the two versions differ. |
| `ts-sdk-release.yml` | `skyvern-ts/client/package.json` | Publishes `@skyvern/client` to npm. |
| `auto-release.yml` | `pyproject.toml` | Creates the GitHub release, then builds and pushes the Docker images tagged `v<version>` and `latest`. |

## Do not bump the version here

`pyproject.toml`, `uv.lock`, `skyvern/` and `skyvern-ts/` are copied into this repo by a sync from the
cloud repo (`Skyvern-AI/skyvern-cloud`). Each sync overwrites this repo's copies. A version bumped here
first is put back by the next sync that touches one of those files:

- 1.0.58 was bumped here on 2026-11-15 and a sync turned it back to 1.0.57 seven hours later.
- 1.0.57 was turned back to 1.0.56 28 minutes after its release, and restored a day later.

Each flip is a version change on `main`, so `auto-release.yml` runs again: it rewrites the notes of an
existing GitHub release and rebuilds its Docker tags from whatever `main` holds at that moment.

Maintainers run the `bump-version` skill in the cloud repo. It covers both repos, in order.

## The one step in this repo

`packages/skyvern-ui/pyproject.toml` exists only here, so it is bumped here, and first.

1. The version is `$ARGUMENTS`. If it is empty, ask. It must match `^\d+\.\d+\.\d+$`.
2. Create a branch named `bump-skyvern-ui-{version}`.
3. Set `version` on line 3 of `packages/skyvern-ui/pyproject.toml` to the new version. Change no other file.
4. Commit, push and open a PR titled `Bump skyvern-ui to {version}`.
5. Tell the user: merge this PR before the cloud repo's bump. `sdk-release.yml` refuses to build when
   `skyvern-ui` and `skyvern` differ, and it runs when the cloud bump arrives.

Nothing is published when this PR merges: no release workflow watches this file.

## Rules

- Never change the version in `pyproject.toml`, `uv.lock`, `skyvern-ts/client/package.json`,
  `skyvern-ts/client/package-lock.json` or `skyvern/client/` in this repo.
- Never push to `main` and never force-push.
- If a step fails, stop, show the error and ask how to proceed.
