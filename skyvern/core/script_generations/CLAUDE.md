# Script Generation & Caching

## Overview

Script generation converts workflow runs into executable Python code that can be cached and reused. This enables "run with code" mode where workflows execute via cached scripts instead of the AI agent.

## Key Files

| File | Purpose |
|------|---------|
| `generate_script.py` | Generates Python code from workflow run data |
| `transform_workflow_run.py` | Transforms DB workflow run into code gen input |
| `skyvern/services/workflow_script_service.py` | Caching logic, script storage |
| `skyvern/forge/sdk/workflow/service.py` | Regeneration decision logic (`generate_script_if_needed`) |

## Key Constants

- `SCRIPT_TASK_BLOCKS` - Block types that have task_id and actions (task, navigation, extraction, etc.)
- `BLOCK_TYPES_THAT_SHOULD_BE_CACHED` in `workflow/service.py` - Block types eligible for caching (includes for_loop)

## How Caching Works

1. **Block execution tracking** (service.py:1309-1316): When a block executes via agent and completes, it's added to `blocks_to_update`
2. **Regeneration decision** (`generate_script_if_needed`): Decides whether to regenerate based on `blocks_to_update` and `missing_labels`
3. **Script generation** (`generate_workflow_script`): Generates code only for blocks that executed this run
4. **Progressive caching**: Only executed blocks are cached; unexecuted blocks remain uncached until they run

## Script Block Requirements for `run_with: code`

For a workflow to execute with cached scripts (`run_with: code`), ALL top-level blocks must have:
1. A `script_block` database entry
2. A non-null `run_signature` field

Without these, the system falls back to `run_with: agent`.

## Critical: Two Mechanisms for Detecting New Blocks

| Mechanism | Location | What it catches |
|-----------|----------|-----------------|
| Execution tracking | service.py:1316 | Blocks that EXECUTED and aren't cached |
| `missing_labels` check | service.py:3436-3441 | Blocks in DEFINITION that aren't cached |

For workflows WITHOUT conditionals, these are equivalent.
For workflows WITH conditionals, they differ - see "Conditional Blocks" below.

## Conditional Blocks

Conditional blocks (`BlockType.CONDITIONAL`) are **NOT cached** - they always run via agent to evaluate conditions at runtime. However, cacheable blocks inside conditional branches ARE cached when they execute.

### Key Insight: Progressive Branch Caching

With conditionals, not all branches execute in a single run. The caching system handles this via "progressive caching":
- Run 1 takes branch A → caches blocks from A
- Run 2 takes branch B → caches blocks from B (preserves A's cache)
- Eventually all executed branches have cached blocks

This means the workflow DEFINITION has all blocks, but the workflow RUN only executes some blocks.

## Performance Optimizations

### Batch Task and Action Queries
**Location**: `transform_workflow_run.py`

Previously, the code made N+1 queries: one `get_task()` and one `get_task_actions_hydrated()` per task block. For workflows with 20 blocks, this meant 40 DB queries.

Now we batch all queries upfront:
1. Collect all task_ids from workflow_run_blocks
2. Single `get_tasks_by_ids()` call for all tasks
3. Single `get_tasks_actions()` call for all actions
4. Process blocks using pre-fetched data from dictionaries

**Impact**: Reduces from 2N queries to 2 queries.

### Block-Level Script Generation
**Location**: `service.py:_generate_pending_script_for_block()`

Previously, `generate_or_update_pending_workflow_script()` was called after each action (CLICK, INPUT_TEXT, etc.), generating "pending" script drafts ~10-50x per workflow run.

Now script generation happens at block completion via `_generate_pending_script_for_block()`, called from both `_execute_workflow_blocks()` and `_execute_workflow_blocks_dag()`.

**Impact**: Reduces script generation frequency by 10-50x while maintaining progressive updates.

## Adding New Cacheable Block Types

When adding a new block type that should support cached execution:
1. Add to `BLOCK_TYPES_THAT_SHOULD_BE_CACHED` in `workflow/service.py`
2. Add handling in `generate_workflow_script_python_code()` with BOTH:
   - `create_or_update_script_block()` - stores metadata in database
   - `append_block_code(block_code)` - adds code to generated script output
3. Ensure `run_signature` is set (the code statement to execute the block)

**CRITICAL**: Every block type needs BOTH database entry AND script output. Missing `append_block_code()` causes runtime failures even if database entries exist.

## Block Processing Order in generate_script.py

1. `task_v1_blocks` - Blocks in `SCRIPT_TASK_BLOCKS`
2. `task_v2_blocks` - task_v2 blocks with child blocks
3. `for_loop_blocks` - ForLoop container blocks
4. `__start_block__` - Workflow entry point

## Things to Watch Out For

1. **Definition vs Execution**: The workflow DEFINITION has all blocks; the workflow RUN only executes some blocks (especially with conditionals)

2. **`blocks_to_update` sources**: This set is populated from multiple places - block execution (line 1316), finalize logic, explicit requests. Understand all sources before modifying.

3. **Database operations per regeneration**: Each regeneration does DELETE + CREATE + UPLOAD + INSERT. Unnecessary regenerations can flood the database.

4. **`BLOCK_TYPES_THAT_SHOULD_BE_CACHED`**: Not all block types are cached. Conditional, wait, code blocks etc. are excluded.

5. **Batch query data mapping**: When using `tasks_by_id` and `actions_by_task_id` dicts, ensure task_ids are consistent between run_blocks and the queried data.

## Testing Caching Changes

When modifying regeneration or caching logic, test these scenarios:

1. **Same blocks run twice** - Should NOT regenerate on 2nd run
2. **New block added** - Should regenerate to include new block
3. **Workflow with conditionals** - Different branches should cache progressively
4. **Block type not in `BLOCK_TYPES_THAT_SHOULD_BE_CACHED`** - Should NOT trigger caching

## Test Commands

```bash
# Run script-related tests
python -m pytest tests/unit/ -k "script" --ignore=tests/unit/test_security.py -v

# Run conditional caching tests specifically
python -m pytest tests/unit/test_conditional_script_caching.py -v

# Run forloop script tests
python -m pytest tests/unit/test_forloop_script_generation.py -v
```
