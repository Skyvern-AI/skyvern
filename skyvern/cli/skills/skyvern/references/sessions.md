# Session Reuse

This guidance is about **runtime session reuse** — passing a `pbs_*` ID as `browser_session_id`
to `skyvern_workflow_run` or `skyvern_run_task` so a one-off run continues a browser that is
already open.

It is NOT about the workflow-level "Save & Reuse Session" toggle (`persist_browser_session`).
Leave that toggle unset unless the user explicitly asks for cross-run state retention — it
defaults to off and should stay off.

Blocks inside a single workflow already share one browser session automatically — do NOT
pass `browser_session_id` to keep state between blocks of the same run. Reach for runtime
session reuse only across separate runs.

## When to reuse a runtime session

- A follow-up task that depends on state from a session you just opened.
- Chained workflow runs where a later run needs the authenticated state from the first.

## When to start fresh

- Session appears invalid or expired.
- Site has strict anti-automation lockouts.
- Running independent tasks in parallel.

## Validation step

After login, run `skyvern_validate` with a concrete condition:
- user avatar visible,
- logout button present,
- account dashboard heading shown.
