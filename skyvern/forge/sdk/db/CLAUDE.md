# Database Layer

## `get_tasks_actions` — Sort Order is DESC (Intentional)

`get_tasks_actions()` returns actions in **descending** `created_at` order. This is intentional — do NOT change it to ascending.

**Why:** The primary consumer is `get_workflow_run_timeline()` in `service.py`, which feeds the frontend timeline UI. The frontend renders actions with `index={actions.length - index}` and expects newest-first (DESC) ordering for correct display numbering.

**Script generation** (`transform_workflow_run.py`) needs chronological (ascending) order for code generation. It reverses the result with `all_actions.reverse()` after fetching.

**History:** PR #8551 changed this to ASC for script gen correctness but broke the timeline. PR #8606 synced the change to cloud. This was fixed by reverting to DESC and reversing in the script gen caller instead.
