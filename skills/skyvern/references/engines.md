# Choosing Between `run_task` and Workflows

Default to workflows for real automation work. Reach for `skyvern_run_task` only when you are
doing a throwaway trial and do not want to keep the result.

## Prefer `skyvern_workflow_create`

- The task spans multiple pages.
- The user says `set this up`, `automate`, `workflow`, `reusable`, `repeat`, or `schedule`.
- You want block-level observability, reruns, parameters, or cached scripts.
- You expect to debug or hand the automation to someone else later.

## Prefer `skyvern_run_task`

- You need a one-off exploratory trial right now.
- The result is disposable and not worth saving.
- You are checking feasibility before deciding whether to build a workflow.

Rule of thumb: if the task crosses page boundaries or sounds like real automation instead of a trial,
build a workflow first.
