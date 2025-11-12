## Overview

Skyvern workflows currently execute blocks strictly in sequence: block _n_ runs after block _n − 1_ and there is only one valid path through the workflow definition. This spec introduces the first phase of a DAG-oriented workflow model by adding a conditional block that can branch to multiple successors. All block types gain an optional `next_block_label` pointer, while conditional blocks hold a list of branch edges that choose the next block at runtime. The system must continue to accept existing list-based workflow definitions so users can adopt DAG capabilities incrementally.

## Goals

- Represent workflow control flow as a graph where each block may point to a next block, and conditional blocks fan out through branch edges.
- Introduce a `BranchCondition` model that captures `if / elif / elif / else` semantics with ordered evaluation and optional `next_block_label`.
- Update workflow storage, APIs, SDKs, and execution so both sequential and DAG-style definitions run without breaking current customers.
- Provide validation, authoring affordances, and observability to help users design and debug branching workflows.

## Non-Goals

- Redesigning the workflow editor UI beyond what is required to author conditional branches and optional next pointers.
- Implementing loop/retry constructs or arbitrary parallel fan-out execution (this spec only covers single-path branching).
- Changing how workflow parameters, scripts, or caching behave beyond what is necessary to route control flow.

## Architecture

### Data Model Updates

- Extend the base `Block` model in `skyvern/forge/sdk/workflow/models/block.py` (and all DTO subclasses) with:
  - `id: str` (existing internal identifier) used for persistence.
  - `label: str` (existing, author-controlled) used as the stable node reference in the DAG; must be unique per workflow.
  - `next_block_label: str | None = None` referencing another block label in the same workflow.
  - `metadata.graph_coordinates` (optional) to aid editors in laying out DAG nodes; nullable for back-compat.
- Introduce `ConditionalBlock` (builds on existing block hierarchy) with `branches: list[BranchCondition]`.
- Define `BranchCondition` as a first-class dataclass / pydantic model with:
  - `id: str` for diff-friendly updates.
  - `criteria: BranchCriteria | None` describing the Boolean condition. `None` marks the `else` branch.
  - `next_block_label: str | None` to jump to another block when the condition matches. `None` indicates the workflow should terminate after the branch.
  - `description: str | None` for editor display.
  - `order: int` to enforce deterministic `if/elif` evaluation order.
  - `is_default: bool` convenience flag to identify the `else` branch (must be unique per conditional block).
- `BranchCriteria` supports a declarative DSL or structured config that can reference workflow parameters, previous block outputs, environment facts, and optional LLM evaluations (e.g., boolean classification prompts). Initial implementation may reuse existing expression evaluators (e.g., Jinja templates, JSONLogic, or pythonic expressions) but must be explicit about supported operators and provide hooks for invoking an LLM-based evaluator when configured.

### Serialization & Backwards Compatibility

- Persist new fields in workflow YAML/JSON definitions, REST/Fern schemas, generated SDKs, and database rows.
- When loading legacy workflows (list of blocks without `next_block_label`), auto-populate an in-memory chain by following list order so execution behaves exactly as today.
- When saving workflows authored in the legacy shape (no explicit `next_block_label`), continue to accept the flat list and omit DAG metadata in the stored payload to avoid churn.
- Ensure API responses always include `next_block_label` (explicit or inferred) so modern clients can treat the workflow as a graph.
- Migrations: if workflows are stored in the database as JSON blobs, no table migration is required beyond ensuring serializers default the new fields to `null`. If relational tables exist, add nullable columns with default `NULL`.

### Branch Evaluation Semantics

- Evaluate branch conditions top-to-bottom by `order` to mimic `if/elif/elif/.../else`.
- `criteria` evaluates inside an execution context containing workflow params, accumulated block outputs, system variables, and (optionally) results from LLM boolean evaluators. Criteria must be side-effect free and return truthy/falsey values regardless of the underlying evaluator.
- Exactly one branch fires per conditional block:
  1. Iterate ordered branches until one returns `True`.
  2. If none match, fall back to the branch where `is_default=True`. Validation must ensure exactly one default exists (or zero if the author wants a “drop out” when nothing matches).
  3. If no branch matches and no default exists, log a warning, mark the workflow as failed (configurable), and stop execution to avoid silent drops.
- The chosen branch’s `next_block_label` determines the next block. `None` means the workflow ends successfully after executing the conditional block.

### Workflow Execution Engine

- Refactor the executor to walk a DAG:
  - Maintain `current_block_label`, starting from a new `workflow.entry_block_label` (defaults to the first block for legacy workflows).
  - After each block completes, prefer `block.next_block_label` if set. Conditional blocks ignore `next_block_label` and instead use the branch result.
  - Detect cycles at validation time; runtime should guard against infinite loops by tracking visited blocks and aborting with a descriptive error if validation was bypassed.
- Update persistence so workflow run records capture both the block ID and the branch taken for auditing/debugging.
- Any dependency resolution (e.g., fetching outputs from previous blocks) now uses graph traversal rather than implicit list indexes.
- Failure handling: if a block fails, preserve existing retry/rollback semantics. Branching does not change error propagation rules.

### Script Generation & Caching

- Update script generation so the emitted `run_workflow` (and any block-level helper functions) encode `branches` as explicit `if/elif/elif/.../else` statements based on the ordered `BranchCondition` list. Each branch condition must be rendered into executable code that evaluates the configured `BranchCriteria` (parameter comparisons, previous block output checks, or LLM boolean calls) and routes to the referenced `next_block_label`.
- Cache keys must remain unique per block label. When the workflow runner encounters a block whose label has no cached script entry, create a new script block after the run completes using the recorded block definition and resolved branch logic.
- After each workflow run, reconcile the executed block graph against the cached script store to discover newly visited labels (including branches that may not execute every run) and persist their corresponding code artifacts so future runs can short-circuit via caching.
- Ensure regenerated scripts remain deterministic even when multiple branches share downstream blocks; caching should key on the tuple `(workflow_permanent_id, block_label)` to avoid collisions between legacy sequential runs and new DAG paths.

### Authoring & Tooling

- Workflow editor (web and CLI) must allow:
  - Setting or clearing `next_block_label` for any block.
  - Converting an existing block into a conditional block with ordered branches and marking one as default (`else`).
  - Visualizing outgoing edges so users understand the DAG.
- Provide helper commands/utilities to auto-wire `next_block_label` for legacy workflows to minimize author effort.
- Documentation must explain how to define branch criteria, including examples for parameter checks, previous block comparisons, and LLM-evaluated prompts.

### Validation & Observability

- Add graph validation to ensure:
  - All referenced `next_block_label` values point to existing blocks.
  - The graph is acyclic (DAG) or, at minimum, that cycles are explicitly disallowed with clear errors.
  - Each conditional block has at most one `else` branch and strictly increasing `order` values.
  - Entry block is defined and reachable.
- Extend logging/metrics to include which branch path was taken, enabling path analytics.
- Surface validation errors to API/UI clients with actionable messages (e.g., “Branch order must be unique per block”).

## Testing Strategy

- Unit tests for data model serialization/deserialization covering `next_block_label`, branch ordering, and default branch handling.
- Executor unit tests that:
  - Run legacy sequential workflows and confirm behavior is unchanged.
  - Exercise multi-branch conditionals, including `else`, missing matches, and terminal branches.
  - Validate cycle detection and error reporting.
- Integration test that loads a mixed workflow (sequential blocks feeding into a conditional block with two branches) and verifies both branches can be executed depending on input parameters.
- UI/e2e tests to ensure users can author branches, configure default behavior, and that persisted workflows reopen with the correct DAG wiring.

## Open Questions

1. Should we introduce a `workflow.entry_block_label` field now, or infer the first block dynamically until multiple entry points are supported?
2. What evaluation options should `BranchCriteria` support (existing template engine, JSONLogic, CEL, bespoke DSL, LLM prompts), and how do we sandbox them for security?
3. Do we need to expose branch-level analytics (counts, success/failure) in the short term, or is logging sufficient for the first iteration?
4. How should we serialize workflows that intentionally omit `next_block_label` to signal “stop after this block” versus leaving it `None` accidentally? Consider explicit sentinel values or validation rules.
