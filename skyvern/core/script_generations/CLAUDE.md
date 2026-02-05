# Script Generation Context

## Key Constants

- `SCRIPT_TASK_BLOCKS` - Block types that have task_id and actions (task, navigation, extraction, etc.)
- `BLOCK_TYPES_THAT_SHOULD_BE_CACHED` in `workflow/service.py` - Block types eligible for caching (includes for_loop)

## Script Block Requirements for `run_with: code`

For a workflow to execute with cached scripts (`run_with: code`), ALL top-level blocks must have:
1. A `script_block` database entry
2. A non-null `run_signature` field

Without these, the system falls back to `run_with: agent`.

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
