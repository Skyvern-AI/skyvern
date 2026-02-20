# Engine Choice for Quick Automation

Use one-off tools by default for short tasks.

## Prefer `skyvern_run_task`

- You need a throwaway automation now.
- The task can complete in a small number of steps.
- Reusability is not required.

## Prefer a workflow instead

- The task will be rerun with different parameters.
- You need branching, loops, or explicit block-level observability.
- You need reproducible runs for operations teams.

Rule of thumb: if you need to run the same automation twice with different inputs, move to `building-workflows`.
