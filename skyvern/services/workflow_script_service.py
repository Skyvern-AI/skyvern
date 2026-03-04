import base64
import hashlib
import re
import urllib.parse
from typing import Any

import structlog
from cachetools import TTLCache
from jinja2.sandbox import SandboxedEnvironment

from skyvern.config import settings
from skyvern.core.script_generations.generate_script import ScriptBlockSource, generate_workflow_script_python_code
from skyvern.core.script_generations.transform_workflow_run import transform_workflow_run_to_code_gen_input
from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.workflow.models.block import get_all_blocks
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowRun
from skyvern.schemas.scripts import FileEncoding, Script, ScriptFileCreate, ScriptStatus
from skyvern.schemas.workflows import BlockType
from skyvern.services import script_service

LOG = structlog.get_logger()
jinja_sandbox_env = SandboxedEnvironment()


def _jinja_domain_filter(url: str) -> str:
    """Extract the domain (netloc) from a URL for use as a cache key grouping.

    Examples:
        "https://jobs.lever.co/textnow/abc" → "jobs.lever.co"
        "https://boards.greenhouse.io/robinhood/jobs/123" → "boards.greenhouse.io"
    """
    try:
        return urllib.parse.urlparse(str(url)).netloc or str(url)
    except Exception:
        return str(url)


jinja_sandbox_env.filters["domain"] = _jinja_domain_filter


def _extract_first_block_domain(workflow: Workflow, parameters: dict[str, Any]) -> str:
    """Extract the domain from the first block that has a URL field.

    Used to automatically enrich the cache key with the target domain so that
    the same workflow running against different sites gets separate cached scripts.
    Returns empty string if no block URL is found.
    """
    try:
        blocks = get_all_blocks(workflow.workflow_definition.blocks)
        for block in blocks:
            url_template = getattr(block, "url", None)
            if not url_template:
                continue
            rendered_url = jinja_sandbox_env.from_string(str(url_template)).render(parameters)
            if rendered_url:
                domain = _jinja_domain_filter(rendered_url)
                if domain:
                    return domain
    except Exception:
        pass
    return ""


def workflow_has_conditionals(workflow: Workflow) -> bool:
    """
    Check if a workflow contains any conditional blocks.

    This is used to determine whether "missing" blocks in the cache should trigger
    regeneration. For workflows with conditionals, blocks in unexecuted branches
    are legitimately missing and should NOT trigger regeneration.
    """
    try:
        all_blocks = get_all_blocks(workflow.workflow_definition.blocks)
        return any(block.block_type == BlockType.CONDITIONAL for block in all_blocks)
    except Exception:
        LOG.warning(
            "Failed to check workflow for conditional blocks",
            workflow_id=workflow.workflow_id,
            exc_info=True,
        )
        return False


# Cache for workflow scripts - only stores non-None results
_workflow_script_cache: TTLCache[tuple, "Script"] = TTLCache(maxsize=128, ttl=60 * 60)


def _make_workflow_script_cache_key(
    organization_id: str,
    workflow_permanent_id: str,
    cache_key_value: str,
    workflow_run_id: str | None = None,
    cache_key: str | None = None,
    statuses: list[ScriptStatus] | None = None,
) -> tuple:
    """Create a hashable cache key from the function arguments."""
    # Convert list to tuple for hashability
    statuses_key = tuple(statuses) if statuses else None
    return (organization_id, workflow_permanent_id, cache_key_value, workflow_run_id, cache_key, statuses_key)


def clear_workflow_script_cache(
    organization_id: str,
    workflow_permanent_id: str | None = None,
) -> int:
    """
    Clear in-memory cached scripts for a workflow or all workflows in an organization.

    Args:
        organization_id: The organization ID to clear cache for.
        workflow_permanent_id: Optional workflow permanent ID. If None, clears all workflows.

    Returns:
        The number of cache entries cleared.
    """
    keys_to_delete = []

    for key in list(_workflow_script_cache.keys()):
        # Key format: (org_id, workflow_permanent_id, cache_key_value, workflow_run_id, cache_key, statuses_key)
        if len(key) >= 2 and key[0] == organization_id:
            if workflow_permanent_id is None or key[1] == workflow_permanent_id:
                keys_to_delete.append(key)

    for key in keys_to_delete:
        _workflow_script_cache.pop(key, None)

    LOG.info(
        "Cleared workflow script in-memory cache",
        organization_id=organization_id,
        workflow_permanent_id=workflow_permanent_id,
        cleared_count=len(keys_to_delete),
    )

    return len(keys_to_delete)


async def generate_or_update_pending_workflow_script(
    workflow_run: WorkflowRun,
    workflow: Workflow,
) -> None:
    organization_id = workflow.organization_id
    context = skyvern_context.current()
    if not context:
        return
    script_id = context.script_id
    script = None
    if script_id:
        script = await app.DATABASE.get_script(script_id=script_id, organization_id=organization_id)

    if not script:
        script = await app.DATABASE.create_script(organization_id=organization_id, run_id=workflow_run.workflow_run_id)
        if context:
            context.script_id = script.script_id
            context.script_revision_id = script.script_revision_id

    _, rendered_cache_key_value = await get_workflow_script(
        workflow=workflow,
        workflow_run=workflow_run,
        status=ScriptStatus.pending,
    )
    await generate_workflow_script(
        workflow_run=workflow_run,
        workflow=workflow,
        script=script,
        rendered_cache_key_value=rendered_cache_key_value,
        pending=True,
        cached_script=script,
    )


async def get_workflow_script(
    workflow: Workflow,
    workflow_run: WorkflowRun,
    block_labels: list[str] | None = None,
    status: ScriptStatus = ScriptStatus.published,
) -> tuple[Script | None, str]:
    """
    Check if there's a related workflow script that should be used instead of running the workflow.
    Returns the tuple of (script, rendered_cache_key_value).
    """
    cache_key = workflow.cache_key or ""
    rendered_cache_key_value = ""

    try:
        parameter_tuples = await app.DATABASE.get_workflow_run_parameters(
            workflow_run_id=workflow_run.workflow_run_id,
        )
        parameters = {wf_param.key: run_param.value for wf_param, run_param in parameter_tuples}

        rendered_cache_key_value = jinja_sandbox_env.from_string(cache_key).render(parameters)

        # Auto-enrich with domain when using the default cache key.
        # This ensures the same workflow running against different sites gets
        # separate cached scripts (e.g., "default:fanr.gov.ae" vs "default:search.gov.hk").
        if rendered_cache_key_value in ("default", ""):
            domain = _extract_first_block_domain(workflow, parameters)
            if domain:
                rendered_cache_key_value = (
                    f"{rendered_cache_key_value}:{domain}" if rendered_cache_key_value else domain
                )

        if block_labels:
            # Do not generate script or run script if block_labels is provided
            return None, rendered_cache_key_value

        # Check if there are existing cached scripts for this workflow + cache_key_value
        existing_script = await get_workflow_script_by_cache_key_value(
            organization_id=workflow.organization_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            cache_key_value=rendered_cache_key_value,
            statuses=[status],
            use_cache=True,
        )

        if existing_script:
            LOG.info(
                "Found cached script for workflow",
                workflow_id=workflow.workflow_id,
                cache_key_value=rendered_cache_key_value,
                workflow_run_id=workflow_run.workflow_run_id,
            )
            return existing_script, rendered_cache_key_value

        return None, rendered_cache_key_value

    except Exception as e:
        LOG.warning(
            "Failed to check for workflow script, proceeding with normal workflow execution",
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            error=str(e),
            exc_info=True,
        )
        return None, rendered_cache_key_value


async def get_workflow_script_by_cache_key_value(
    organization_id: str,
    workflow_permanent_id: str,
    cache_key_value: str,
    workflow_run_id: str | None = None,
    cache_key: str | None = None,
    statuses: list[ScriptStatus] | None = None,
    use_cache: bool = False,
) -> Script | None:
    if use_cache:
        cache_key_tuple = _make_workflow_script_cache_key(
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
            cache_key_value=cache_key_value,
            workflow_run_id=workflow_run_id,
            cache_key=cache_key,
            statuses=statuses,
        )
        # Check cache first
        if cache_key_tuple in _workflow_script_cache:
            return _workflow_script_cache[cache_key_tuple]

        # Cache miss - fetch from database
        result = await app.DATABASE.get_workflow_script_by_cache_key_value(
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
            cache_key_value=cache_key_value,
            workflow_run_id=workflow_run_id,
            cache_key=cache_key,
            statuses=statuses,
        )

        # Only cache non-None results
        if result is not None:
            _workflow_script_cache[cache_key_tuple] = result

        return result

    return await app.DATABASE.get_workflow_script_by_cache_key_value(
        organization_id=organization_id,
        workflow_permanent_id=workflow_permanent_id,
        cache_key_value=cache_key_value,
        workflow_run_id=workflow_run_id,
        cache_key=cache_key,
        statuses=statuses,
    )


async def _load_cached_script_block_sources(
    script: Script,
    organization_id: str,
) -> dict[str, ScriptBlockSource]:
    """
    Load existing script block sources (code + metadata) for a script revision so they can be reused.
    """
    cached_blocks: dict[str, ScriptBlockSource] = {}

    script_blocks = await app.DATABASE.get_script_blocks_by_script_revision_id(
        script_revision_id=script.script_revision_id,
        organization_id=organization_id,
    )

    for script_block in script_blocks:
        if not script_block.script_block_label:
            continue

        code_str: str | None = None
        if script_block.script_file_id:
            script_file = await app.DATABASE.get_script_file_by_id(
                script_revision_id=script.script_revision_id,
                file_id=script_block.script_file_id,
                organization_id=organization_id,
            )
            if script_file and script_file.artifact_id:
                artifact = await app.DATABASE.get_artifact_by_id(script_file.artifact_id, organization_id)
                if artifact:
                    file_content = await app.ARTIFACT_MANAGER.retrieve_artifact(artifact)
                    if isinstance(file_content, bytes):
                        code_str = file_content.decode("utf-8")
                    elif isinstance(file_content, str):
                        code_str = file_content

        if not code_str:
            continue

        cached_blocks[script_block.script_block_label] = ScriptBlockSource(
            label=script_block.script_block_label,
            code=code_str,
            run_signature=script_block.run_signature,
            workflow_run_id=script_block.workflow_run_id,
            workflow_run_block_id=script_block.workflow_run_block_id,
            input_fields=script_block.input_fields,
            requires_agent=script_block.requires_agent,
        )

    return cached_blocks


async def generate_workflow_script(
    workflow_run: WorkflowRun,
    workflow: Workflow,
    script: Script,
    rendered_cache_key_value: str,
    pending: bool = False,
    cached_script: Script | None = None,
    updated_block_labels: set[str] | None = None,
) -> None:
    # Note: Workflows with conditional blocks ARE supported. The conditional block itself
    # is not cached (it's evaluated at runtime), but cacheable blocks in branches are
    # cached progressively as they execute. See workflow_has_conditionals() for the
    # regeneration logic that prevents unnecessary regeneration for unexecuted branches.
    try:
        LOG.info(
            "Generating script for workflow",
            workflow_run_id=workflow_run.workflow_run_id,
            workflow_id=workflow.workflow_id,
            workflow_name=workflow.title,
            cache_key_value=rendered_cache_key_value,
        )
        cached_block_sources: dict[str, ScriptBlockSource] = {}
        if cached_script:
            cached_block_sources = await _load_cached_script_block_sources(cached_script, workflow.organization_id)

        codegen_input = await transform_workflow_run_to_code_gen_input(
            workflow_run_id=workflow_run.workflow_run_id,
            organization_id=workflow.organization_id,
        )

        block_labels = [block.get("label") for block in codegen_input.workflow_blocks if block.get("label")]

        if updated_block_labels is None:
            updated_block_labels = {label for label in block_labels if label}
        else:
            updated_block_labels = set(updated_block_labels)

        missing_labels = {label for label in block_labels if label and label not in cached_block_sources}
        updated_block_labels.update(missing_labels)
        updated_block_labels.add(settings.WORKFLOW_START_BLOCK_LABEL)

        python_src = await generate_workflow_script_python_code(
            file_name=codegen_input.file_name,
            workflow_run_request=codegen_input.workflow_run,
            workflow=codegen_input.workflow,
            blocks=codegen_input.workflow_blocks,
            actions_by_task=codegen_input.actions_by_task,
            task_v2_child_blocks=codegen_input.task_v2_child_blocks,
            organization_id=workflow.organization_id,
            script_id=script.script_id,
            script_revision_id=script.script_revision_id,
            pending=pending,
            cached_blocks=cached_block_sources,
            updated_block_labels=updated_block_labels,
            use_semantic_selectors=workflow.adaptive_caching,
            adaptive_caching=workflow.adaptive_caching,
        )
    except Exception:
        LOG.error("Failed to generate workflow script source", exc_info=True)
        return

    # 4) Persist script and files, then record mapping
    content_bytes = python_src.encode("utf-8")
    content_b64 = base64.b64encode(content_bytes).decode("utf-8")
    files = [
        ScriptFileCreate(
            path="main.py",
            content=content_b64,
            encoding=FileEncoding.BASE64,
            mime_type="text/x-python",
        )
    ]

    # Upload script file(s) as artifacts and create rows
    await script_service.build_file_tree(
        files=files,
        organization_id=workflow.organization_id,
        script_id=script.script_id,
        script_version=script.version,
        script_revision_id=script.script_revision_id,
        pending=pending,
    )

    # check if an existing draft workflow script exists for this workflow run
    existing_pending_workflow_script = None
    status = ScriptStatus.published
    if pending:
        status = ScriptStatus.pending
        existing_pending_workflow_script = await app.DATABASE.get_workflow_script(
            organization_id=workflow.organization_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            workflow_run_id=workflow_run.workflow_run_id,
            statuses=[status],
        )
    if not existing_pending_workflow_script:
        # Record the workflow->script mapping for cache lookup
        await app.DATABASE.create_workflow_script(
            organization_id=workflow.organization_id,
            script_id=script.script_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            cache_key=workflow.cache_key or "",
            cache_key_value=rendered_cache_key_value,
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            status=status,
        )


_IMPORT_RE = re.compile(r"^(?:import |from \S+ import )")


def _split_imports(code: str) -> tuple[list[str], str]:
    """Split leading import statements from reviewer-generated code.

    Returns (import_lines, remaining_code) where import_lines are any
    ``import ...`` or ``from ... import ...`` lines that appear before the
    first non-import, non-blank line (i.e. the ``@skyvern.cached`` decorator
    or ``async def``).
    """
    code_lines = code.split("\n")
    imports: list[str] = []
    body_start = 0
    for i, line in enumerate(code_lines):
        stripped = line.strip()
        if not stripped:
            continue
        if _IMPORT_RE.match(stripped):
            imports.append(stripped)
            body_start = i + 1
        else:
            break
    # Skip blank lines between imports and function body
    while body_start < len(code_lines) and not code_lines[body_start].strip():
        body_start += 1
    return imports, "\n".join(code_lines[body_start:])


def _hoist_imports(lines: list[str], new_imports: list[str]) -> None:
    """Insert new import lines into the file's existing import section (mutates *lines*).

    Imports are deduplicated against existing lines and inserted after the last
    existing top-level import statement.
    """
    if not new_imports:
        return
    existing = {line.strip() for line in lines}
    # Deduplicate against both existing file lines and within new_imports itself
    seen: set[str] = set()
    to_add: list[str] = []
    for imp in new_imports:
        if imp not in existing and imp not in seen:
            to_add.append(imp)
            seen.add(imp)
    if not to_add:
        return

    # Find the last top-level import line
    last_import_idx = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if _IMPORT_RE.match(stripped):
            last_import_idx = i
        elif stripped and not stripped.startswith("#") and last_import_idx >= 0:
            # Hit first non-import, non-blank, non-comment line after imports
            break

    insert_at = last_import_idx + 1 if last_import_idx >= 0 else 0
    for offset, imp in enumerate(to_add):
        lines.insert(insert_at + offset, imp)


def _patch_main_py(main_py_content: str, updated_blocks: dict[str, str]) -> str:
    """Replace cached block functions in main.py with updated code from the reviewer.

    Each block function in main.py starts with ``@skyvern.cached(cache_key = 'LABEL')``
    and ends just before the next ``@skyvern.cached`` decorator or the end of the file.
    This function splices in the new code for each updated block.

    If the reviewer output contains leading ``import`` statements, they are hoisted
    to the file's import section so they don't break decorator/function syntax.
    """
    lines = main_py_content.split("\n")

    # Collect imports from all updated blocks first, then hoist once
    all_new_imports: list[str] = []
    cleaned_blocks: dict[str, str] = {}
    for label, code in updated_blocks.items():
        imports, body = _split_imports(code)
        all_new_imports.extend(imports)
        cleaned_blocks[label] = body

    _hoist_imports(lines, all_new_imports)

    # Build index: block_label -> (start_line, end_line) in main.py
    decorator_pattern = re.compile(r"^@skyvern\.cached\(\s*cache_key\s*=\s*['\"]([^'\"]+)['\"]\s*\)")
    block_positions: list[tuple[str, int]] = []  # (label, line_index)
    for i, line in enumerate(lines):
        m = decorator_pattern.match(line.strip())
        if m:
            block_positions.append((m.group(1), i))

    if not block_positions:
        # No existing blocks found — append all updated blocks at the end
        for label, code in cleaned_blocks.items():
            new_code = code.rstrip("\n")
            decorator_line = f"@skyvern.cached(cache_key = '{label}')"
            lines.extend(["", "", decorator_line, new_code])
        return "\n".join(lines)

    # Track which labels were patched (to identify new blocks that need appending)
    patched_labels: set[str] = set()
    existing_labels = {label for label, _ in block_positions}

    # Process in reverse order so line insertions don't shift earlier indices
    for idx in range(len(block_positions) - 1, -1, -1):
        label, start = block_positions[idx]
        if label not in cleaned_blocks:
            continue

        patched_labels.add(label)

        # End of this block = start of next block, or end of file
        if idx + 1 < len(block_positions):
            end = block_positions[idx + 1][1]
        else:
            end = len(lines)

        # Strip trailing blank lines from the replacement to avoid accumulation
        new_code = cleaned_blocks[label].rstrip("\n")

        # Ensure the @skyvern.cached decorator is present so the function gets
        # registered at import time. The reviewer output may or may not include it.
        if not new_code.lstrip().startswith("@skyvern.cached"):
            decorator_line = f"@skyvern.cached(cache_key = '{label}')"
            lines[start:end] = [decorator_line, new_code, "", ""]
        else:
            lines[start:end] = [new_code, "", ""]

    # Append NEW blocks that don't exist in main.py yet (e.g., conditional blocks
    # being upgraded from agent-required to code for the first time).
    for label, code in cleaned_blocks.items():
        if label in existing_labels:
            continue
        new_code = code.rstrip("\n")
        decorator_line = f"@skyvern.cached(cache_key = '{label}')"
        lines.extend(["", "", decorator_line, new_code])

    return "\n".join(lines)


async def _find_main_py_content(script_id: str, organization_id: str, base_revision_id: str) -> str | None:
    """Find main.py content, preferring the base revision then falling back to v1.

    After this fix is deployed, every new version will have a patched main.py, so
    the base revision will always have one. The v1 fallback exists for bootstrapping
    (versions created before this fix existed).
    """

    async def _load_main_py_from_revision(revision_id: str) -> str | None:
        files = await app.DATABASE.get_script_files(
            script_revision_id=revision_id,
            organization_id=organization_id,
        )
        for f in files:
            if f.file_path == "main.py" and f.artifact_id:
                artifact = await app.DATABASE.get_artifact_by_id(f.artifact_id, organization_id)
                if artifact:
                    content = await app.ARTIFACT_MANAGER.retrieve_artifact(artifact)
                    if content:
                        return content.decode("utf-8") if isinstance(content, bytes) else content
        return None

    # Try the base revision first (has cumulative patches from all prior reviews)
    result = await _load_main_py_from_revision(base_revision_id)
    if result:
        return result

    # Fall back to v1 (bootstrapping: base was created before this fix)
    v1_script = await app.DATABASE.get_script(
        script_id=script_id,
        organization_id=organization_id,
        version=1,
    )
    if v1_script and v1_script.script_revision_id != base_revision_id:
        return await _load_main_py_from_revision(v1_script.script_revision_id)

    return None


async def create_script_version_from_review(
    organization_id: str,
    workflow_permanent_id: str,
    base_script: Script,
    updated_blocks: dict[str, str],
    workflow: Workflow,
    workflow_run: WorkflowRun,
    conditional_blocks: dict[str, str] | None = None,
) -> Script | None:
    """Create a new script version incorporating updated block code from the AI reviewer.

    Args:
        organization_id: The organization ID.
        workflow_permanent_id: The workflow permanent ID.
        base_script: The script revision being improved.
        updated_blocks: Dict of {block_label: updated_code} from the reviewer.
        workflow: The workflow model.
        workflow_run: The workflow run that triggered the review.

    Returns:
        The new Script revision, or None if creation failed.
    """
    try:
        # Create a new script version
        new_script = await app.DATABASE.create_script(
            organization_id=organization_id,
            script_id=base_script.script_id,
            version=base_script.version + 1,
            run_id=workflow_run.workflow_run_id,
        )

        # Copy existing script blocks from the base revision
        existing_blocks = await app.DATABASE.get_script_blocks_by_script_revision_id(
            script_revision_id=base_script.script_revision_id,
            organization_id=organization_id,
        )

        conditional_blocks = conditional_blocks or {}

        for sb in existing_blocks:
            if sb.script_block_label in updated_blocks:
                # This block has an updated version from the reviewer
                updated_code = updated_blocks[sb.script_block_label]
                content_bytes = updated_code.encode("utf-8")
                content_hash = hashlib.sha256(content_bytes).hexdigest()
                file_path = f"blocks/{sb.script_block_label}.py"

                # Upload code to S3 and create script file DB record
                artifact_id = await app.ARTIFACT_MANAGER.create_script_file_artifact(
                    organization_id=organization_id,
                    script_id=new_script.script_id,
                    script_version=new_script.version,
                    file_path=file_path,
                    data=content_bytes,
                )
                new_file = await app.DATABASE.create_script_file(
                    script_revision_id=new_script.script_revision_id,
                    script_id=new_script.script_id,
                    organization_id=organization_id,
                    file_path=file_path,
                    file_name=f"{sb.script_block_label}.py",
                    file_type="file",
                    content_hash=f"sha256:{content_hash}",
                    file_size=len(content_bytes),
                    mime_type="text/x-python",
                    artifact_id=artifact_id,
                )

                # Determine if this is a conditional block being upgraded to code.
                # If so, flip requires_agent to False and set the run_signature.
                is_conditional_upgrade = sb.script_block_label in conditional_blocks
                block_requires_agent = False if is_conditional_upgrade else sb.requires_agent
                block_run_signature = (
                    f'await skyvern.conditional(label="{sb.script_block_label}")'
                    if is_conditional_upgrade
                    else sb.run_signature
                )

                # Create script block entry pointing to the new file
                await app.DATABASE.create_script_block(
                    organization_id=organization_id,
                    script_id=new_script.script_id,
                    script_revision_id=new_script.script_revision_id,
                    script_block_label=sb.script_block_label,
                    script_file_id=new_file.file_id,
                    run_signature=block_run_signature,
                    workflow_run_id=workflow_run.workflow_run_id,
                    input_fields=sb.input_fields,
                    requires_agent=block_requires_agent,
                )
            else:
                # Copy existing block as-is
                await app.DATABASE.create_script_block(
                    organization_id=organization_id,
                    script_id=new_script.script_id,
                    script_revision_id=new_script.script_revision_id,
                    script_block_label=sb.script_block_label,
                    script_file_id=sb.script_file_id,
                    run_signature=sb.run_signature,
                    workflow_run_id=sb.workflow_run_id,
                    workflow_run_block_id=sb.workflow_run_block_id,
                    input_fields=sb.input_fields,
                    requires_agent=sb.requires_agent,
                )

        # Patch main.py with updated block functions and copy non-block files.
        # Prefers the base revision's main.py (cumulative patches), falls back to v1.
        main_py_content = await _find_main_py_content(
            script_id=base_script.script_id,
            organization_id=organization_id,
            base_revision_id=base_script.script_revision_id,
        )
        if main_py_content:
            patched_main = _patch_main_py(main_py_content, updated_blocks)
            patched_bytes = patched_main.encode("utf-8")
            patched_hash = hashlib.sha256(patched_bytes).hexdigest()

            main_artifact_id = await app.ARTIFACT_MANAGER.create_script_file_artifact(
                organization_id=organization_id,
                script_id=new_script.script_id,
                script_version=new_script.version,
                file_path="main.py",
                data=patched_bytes,
            )
            await app.DATABASE.create_script_file(
                script_revision_id=new_script.script_revision_id,
                script_id=new_script.script_id,
                organization_id=organization_id,
                file_path="main.py",
                file_name="main.py",
                file_type="file",
                content_hash=f"sha256:{patched_hash}",
                file_size=len(patched_bytes),
                mime_type="text/x-python",
                artifact_id=main_artifact_id,
            )
            LOG.info(
                "Patched main.py with updated block code",
                script_id=new_script.script_id,
                version=new_script.version,
                patched_blocks=list(updated_blocks.keys()),
                main_py_size=len(patched_bytes),
            )

            # Copy non-block files (e.g., .skyvern metadata) from the base revision
            # or v1 — whichever has the full file set
            source_files = await app.DATABASE.get_script_files(
                script_revision_id=base_script.script_revision_id,
                organization_id=organization_id,
            )
            if not any(f.file_path != "main.py" and not f.file_path.startswith("blocks/") for f in source_files):
                # Base revision has no non-block files, fall back to v1
                v1_script = await app.DATABASE.get_script(
                    script_id=base_script.script_id,
                    organization_id=organization_id,
                    version=1,
                )
                if v1_script:
                    source_files = await app.DATABASE.get_script_files(
                        script_revision_id=v1_script.script_revision_id,
                        organization_id=organization_id,
                    )

            updated_block_file_paths = {f"blocks/{label}.py" for label in updated_blocks}
            for f in source_files:
                # Skip main.py (already patched) and updated block files (already created)
                if f.file_path == "main.py" or f.file_path in updated_block_file_paths:
                    continue
                await app.DATABASE.create_script_file(
                    script_revision_id=new_script.script_revision_id,
                    script_id=new_script.script_id,
                    organization_id=organization_id,
                    file_path=f.file_path,
                    file_name=f.file_name,
                    file_type=f.file_type,
                    content_hash=f.content_hash,
                    file_size=f.file_size,
                    mime_type=f.mime_type,
                    artifact_id=f.artifact_id,
                )
        else:
            LOG.warning(
                "Could not find main.py to patch for new script version",
                script_id=base_script.script_id,
                version=new_script.version,
            )

        # Create the workflow script mapping for cache lookup
        _, rendered_cache_key_value = await get_workflow_script(
            workflow=workflow,
            workflow_run=workflow_run,
            status=ScriptStatus.published,
        )

        await app.DATABASE.create_workflow_script(
            organization_id=organization_id,
            script_id=new_script.script_id,
            workflow_permanent_id=workflow_permanent_id,
            cache_key=workflow.cache_key or "",
            cache_key_value=rendered_cache_key_value,
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            status=ScriptStatus.published,
        )

        # Clear the in-memory cache so the new version is picked up
        clear_workflow_script_cache(
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
        )

        LOG.info(
            "Created new script version from AI review",
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
            base_version=base_script.version,
            new_version=new_script.version,
            updated_block_count=len(updated_blocks),
        )

        return new_script

    except Exception:
        LOG.exception(
            "Failed to create script version from review",
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
        )
        return None
