# Session Reuse

## Cloud browser profiles for saved logins

Use a cloud browser profile when the user wants a durable saved login across future runs or sessions.
Browser profiles are identified by `bp_...` and are created explicitly:

```bash
# workflow_run source (server polls internally for up to 30s)
skyvern browser-profile create --name "site-signed-in" --workflow-run-id wr_123

# browser_session source: CLOSE THE SESSION FIRST. The archive only uploads
# after the session closes. Calling create against an open session returns
# ARCHIVE_NOT_READY indefinitely.
skyvern browser session close --id pbs_123
skyvern browser-profile create --name "site-signed-in" --browser-session-id pbs_123
```

For workflow-run sources, persist_browser_session=true is a workflow-definition property. It is not a
`skyvern workflow run` flag. Only wr_ IDs are accepted; tsk_v2_ task IDs are not. A plain
`create_browser_session()` session alone does not create a profile archive; the source session or workflow
run must have a persisted browser archive. Archive creation is asynchronous, so a freshly completed source
can require a short bounded retry before profile creation succeeds.

Reuse profiles on normal cloud entrypoints:

```bash
skyvern workflow run --id wpid_123 --browser-profile-id bp_123
skyvern browser session create --browser-profile-id bp_123
```

After reuse, validate logged-in state before re-login. If the user is already authenticated, skip or
conditionalize login blocks rather than logging in on every run.

`skyvern browser state save/load` and MCP `state_save/state_load` are local/browser-file tools. They are not the
cloud browser-profile reuse path. Product UI is not required for browser-profile save/reuse through MCP or CLI.

## When to reuse a session

- Multiple actions on one authenticated site.
- Workflow chains that depend on retained state.
- Follow-up extraction immediately after login.

## When to start fresh

- Session appears invalid or expired.
- Site has strict anti-automation lockouts.
- Running independent tasks in parallel.

## Validation step

After login, run `skyvern_validate` with a concrete condition:
- user avatar visible,
- logout button present,
- account dashboard heading shown.
