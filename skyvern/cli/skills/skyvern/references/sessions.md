# Session Reuse

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
