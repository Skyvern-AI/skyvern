import base64
import hashlib
import re
import time
import urllib.parse
from collections import deque
from typing import Any, NamedTuple

import structlog
from cachetools import TTLCache
from jinja2.sandbox import SandboxedEnvironment

from skyvern.config import settings
from skyvern.core.script_generations.generate_script import (
    ScriptBlockSource,
    generate_workflow_script_python_code,
)
from skyvern.core.script_generations.transform_workflow_run import transform_workflow_run_to_code_gen_input
from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.workflow.models.block import get_all_blocks
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowRun, is_adaptive_caching
from skyvern.schemas.scripts import FileEncoding, Script, ScriptFileCreate, ScriptStatus
from skyvern.schemas.workflows import BlockType
from skyvern.services import script_service
from skyvern.utils.url_validators import prepend_scheme_and_validate_url

LOG = structlog.get_logger()
jinja_sandbox_env = SandboxedEnvironment()

# Shared regex for parsing @skyvern.cached decorator lines in main.py source.
_CACHED_DECORATOR_RE = re.compile(
    r"^@skyvern\.cached\(\s*cache_key\s*=\s*['\"]([^'\"]+)['\"]\s*\)",
    re.MULTILINE,
)


def extract_cached_blocks_from_source(content: str) -> dict[str, str]:
    """Parse all @skyvern.cached blocks from Python source.

    Returns {block_label: code} for every block found. Each value includes
    the decorator line through to the start of the next decorator (or EOF).
    """
    matches = list(_CACHED_DECORATOR_RE.finditer(content))
    result: dict[str, str] = {}
    for idx, match in enumerate(matches):
        label = match.group(1)
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(content)
        block_code = content[start:end].rstrip()
        if block_code:
            result[label] = block_code
    return result


def extract_single_cached_block(content: str, block_label: str) -> str | None:
    """Extract a single @skyvern.cached block by label from Python source."""
    pattern = re.compile(
        rf"^@skyvern\.cached\(\s*cache_key\s*=\s*['\"]{re.escape(block_label)}['\"]\s*\)",
        re.MULTILINE,
    )
    match = pattern.search(content)
    if not match:
        return None
    start = match.start()
    next_decorator = _CACHED_DECORATOR_RE.search(content[match.end() :])
    end = match.end() + next_decorator.start() if next_decorator else len(content)
    block_code = content[start:end].rstrip()
    return block_code if block_code else None


async def load_main_py_content(
    script_revision_id: str,
    organization_id: str,
) -> str | None:
    """Load and decode main.py content from S3 for a script revision."""
    try:
        script_files = await app.DATABASE.scripts.get_script_files(
            script_revision_id=script_revision_id,
            organization_id=organization_id,
        )
        for f in script_files:
            if f.file_path == "main.py" and f.artifact_id:
                artifact = await app.DATABASE.artifacts.get_artifact_by_id(f.artifact_id, organization_id)
                if not artifact:
                    return None
                raw_content = await app.ARTIFACT_MANAGER.retrieve_artifact(artifact)
                if isinstance(raw_content, bytes):
                    return raw_content.decode("utf-8")
                elif isinstance(raw_content, str):
                    return raw_content
                return None
    except Exception:
        LOG.warning(
            "Failed to load main.py content",
            script_revision_id=script_revision_id,
            exc_info=True,
        )
    return None


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


def _resolve_block_url_for_cache_key(url_template: str, parameters: dict[str, Any]) -> str:
    """Resolve a block ``url`` the same way runtime does: param-key swap,
    Jinja render, prepend-scheme validate. Must stay in sync with
    ``BaseTaskBlock.execute`` / ``format_potential_template_parameters``.
    """
    candidate = url_template
    if url_template in parameters:
        value = parameters[url_template]
        if value:
            candidate = str(value)
    rendered = jinja_sandbox_env.from_string(candidate).render(parameters)
    if not rendered:
        return ""
    return prepend_scheme_and_validate_url(rendered)


def _extract_first_block_domain(workflow: Workflow, parameters: dict[str, Any]) -> str:
    """Extract the domain from the first block's URL for cache-key enrichment.

    Calls ``_resolve_block_url_for_cache_key`` on each block's ``url`` and
    returns the first non-empty domain. The helper mirrors runtime's URL
    resolution — see its docstring for the pipeline.
    """
    try:
        blocks = get_all_blocks(workflow.workflow_definition.blocks)
        for block in blocks:
            url_template = getattr(block, "url", None)
            if not url_template:
                continue
            rendered_url = _resolve_block_url_for_cache_key(str(url_template), parameters)
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
_workflow_script_cache: TTLCache[tuple, tuple["Script", bool]] = TTLCache(maxsize=128, ttl=60 * 60)


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
        script = await app.DATABASE.scripts.get_script(script_id=script_id, organization_id=organization_id)

    if not script:
        script = await app.DATABASE.scripts.create_script(
            organization_id=organization_id, run_id=workflow_run.workflow_run_id
        )
        if context:
            context.script_id = script.script_id
            context.script_revision_id = script.script_revision_id

    _script, rendered_cache_key_value, _is_pinned = await get_workflow_script(
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


async def _invalidate_if_parameters_changed(
    workflow: Workflow,
    existing_script: Script,
    cache_key_value: str,
    workflow_run_id: str,
) -> bool:
    """Return True if the cached script should be invalidated because the
    workflow's parameter key set has changed since the script was generated.

    Only fires when the workflow version id differs from the version that
    produced the cached script, so steady-state cache hits pay no extra DB
    work. A missing prior workflow row (hard-deleted) is treated as a cache
    miss for safety.
    """
    cache_workflow_id = await app.DATABASE.scripts.get_workflow_script_source_workflow_id(
        organization_id=workflow.organization_id,
        workflow_permanent_id=workflow.workflow_permanent_id,
        script_id=existing_script.script_id,
        cache_key_value=cache_key_value,
    )
    if not cache_workflow_id or cache_workflow_id == workflow.workflow_id:
        return False

    old_workflow = await app.DATABASE.workflows.get_workflow(
        workflow_id=cache_workflow_id,
        organization_id=workflow.organization_id,
    )
    if old_workflow is None:
        LOG.info(
            "Cached script invalidated: prior workflow version not found",
            workflow_id=workflow.workflow_id,
            cache_workflow_id=cache_workflow_id,
            script_id=existing_script.script_id,
            workflow_run_id=workflow_run_id,
        )
        return True

    old_param_keys = {p.key for p in old_workflow.workflow_definition.parameters}
    new_param_keys = {p.key for p in workflow.workflow_definition.parameters}
    if old_param_keys != new_param_keys:
        LOG.info(
            "Cached script invalidated: workflow parameter set changed",
            workflow_id=workflow.workflow_id,
            cache_workflow_id=cache_workflow_id,
            script_id=existing_script.script_id,
            workflow_run_id=workflow_run_id,
            added_params=sorted(new_param_keys - old_param_keys),
            removed_params=sorted(old_param_keys - new_param_keys),
        )
        return True

    return False


async def get_workflow_script(
    workflow: Workflow,
    workflow_run: WorkflowRun,
    block_labels: list[str] | None = None,
    status: ScriptStatus = ScriptStatus.published,
) -> tuple[Script | None, str, bool]:
    """
    Check if there's a related workflow script that should be used instead of running the workflow.
    Returns the tuple of (script, rendered_cache_key_value, is_pinned).
    """
    cache_key = workflow.cache_key or ""
    rendered_cache_key_value = ""

    try:
        parameter_tuples = await app.DATABASE.workflow_runs.get_workflow_run_parameters(
            workflow_run_id=workflow_run.workflow_run_id,
        )
        parameters = {wf_param.key: run_param.value for wf_param, run_param in parameter_tuples}

        rendered_cache_key_value = jinja_sandbox_env.from_string(cache_key).render(parameters)

        # Auto-enrich with domain when using the default cache key.
        # This ensures the same workflow running against different sites gets
        # separate cached scripts. For known platform patterns, a platform-level
        # key is used instead of the domain so all employers on the same platform
        # share one cached script.
        if rendered_cache_key_value in ("default", ""):
            domain = _extract_first_block_domain(workflow, parameters)
            if domain:
                ats_platform = app.AGENT_FUNCTION.detect_ats_platform(domain)
                if ats_platform:
                    LOG.info(
                        "Code 2.0: platform detected, using platform-level cache key",
                        ats_platform=ats_platform,
                        original_domain=domain,
                        workflow_permanent_id=workflow.workflow_permanent_id,
                    )
                cache_segment = ats_platform if ats_platform else domain
                rendered_cache_key_value = (
                    f"{rendered_cache_key_value}:{cache_segment}" if rendered_cache_key_value else cache_segment
                )

        # Namespace adaptive caching (Code 2.0) scripts with :v2 suffix so they
        # don't collide with traditional (Code 1.0) cached scripts.
        if is_adaptive_caching(workflow, workflow_run):
            rendered_cache_key_value = f"{rendered_cache_key_value}:v2" if rendered_cache_key_value else "v2"

        LOG.info(
            "Resolved cache key for workflow script lookup",
            cache_key_value=rendered_cache_key_value,
            workflow_permanent_id=workflow.workflow_permanent_id,
            workflow_run_id=workflow_run.workflow_run_id,
        )

        if block_labels:
            # Do not generate script or run script if block_labels is provided
            return None, rendered_cache_key_value, False

        # Check if there are existing cached scripts for this workflow + cache_key_value
        existing_script, is_pinned = await get_workflow_script_by_cache_key_value(
            organization_id=workflow.organization_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            cache_key_value=rendered_cache_key_value,
            statuses=[status],
        )

        if existing_script:
            # SKY-9254: invalidate the cached script when the workflow's parameter
            # set has changed since it was generated. Cache lookup keys on
            # (org, wpid, cache_key_value) — none of which change when a user
            # edits the workflow to add/remove a parameter. Without this check
            # the old cached code (which has no reference to the new param)
            # keeps getting served, and the new param ends up injected wherever
            # the agent guesses.
            invalidated = await _invalidate_if_parameters_changed(
                workflow=workflow,
                existing_script=existing_script,
                cache_key_value=rendered_cache_key_value,
                workflow_run_id=workflow_run.workflow_run_id,
            )
            if invalidated:
                return None, rendered_cache_key_value, False

            LOG.info(
                "Found cached script for workflow (cache hit)",
                workflow_id=workflow.workflow_id,
                script_id=existing_script.script_id,
                cache_key_value=rendered_cache_key_value,
                workflow_run_id=workflow_run.workflow_run_id,
                is_pinned=is_pinned,
            )
            return existing_script, rendered_cache_key_value, is_pinned

        LOG.info(
            "No cached script found for workflow (cache miss)",
            workflow_id=workflow.workflow_id,
            cache_key_value=rendered_cache_key_value,
            workflow_run_id=workflow_run.workflow_run_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
        )
        return None, rendered_cache_key_value, False

    except Exception as e:
        LOG.warning(
            "Failed to check for workflow script, proceeding with normal workflow execution",
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            error=str(e),
            exc_info=True,
        )
        return None, rendered_cache_key_value, False


async def get_workflow_script_by_cache_key_value(
    organization_id: str,
    workflow_permanent_id: str,
    cache_key_value: str,
    workflow_run_id: str | None = None,
    cache_key: str | None = None,
    statuses: list[ScriptStatus] | None = None,
    use_cache: bool = False,
) -> tuple[Script | None, bool]:
    """Look up the best script for a workflow + cache_key_value.

    Returns:
        A tuple of (script, is_pinned) where is_pinned indicates whether the
        returned script came from a pinned workflow_script row.
    """
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
        script, is_pinned = await app.DATABASE.scripts.get_workflow_script_by_cache_key_value(
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
            cache_key_value=cache_key_value,
            workflow_run_id=workflow_run_id,
            cache_key=cache_key,
            statuses=statuses,
        )

        # Only cache non-None results
        if script is not None:
            _workflow_script_cache[cache_key_tuple] = (script, is_pinned)

        return script, is_pinned

    return await app.DATABASE.scripts.get_workflow_script_by_cache_key_value(
        organization_id=organization_id,
        workflow_permanent_id=workflow_permanent_id,
        cache_key_value=cache_key_value,
        workflow_run_id=workflow_run_id,
        cache_key=cache_key,
        statuses=statuses,
    )


async def get_latest_published_script(
    organization_id: str,
    workflow_permanent_id: str,
) -> Script | None:
    """Get the latest published script for a workflow (any cache key value).

    When multiple published workflow scripts exist (e.g. different cache_key_value
    variants), this returns the script with the highest version number to ensure
    the most recently reviewed code is selected.
    """
    workflow_scripts = await app.DATABASE.scripts.get_workflow_scripts_by_permanent_id(
        organization_id=organization_id,
        workflow_permanent_id=workflow_permanent_id,
        statuses=[ScriptStatus.published],
    )
    if not workflow_scripts:
        return None

    # N+1 queries: one per workflow_script. Acceptable because the number of
    # published cache_key_value variants per workflow is typically 1-3.
    # TODO: add a bulk get_latest_script_versions() if this becomes a bottleneck.
    best: Script | None = None
    for ws in workflow_scripts:
        script = await app.DATABASE.scripts.get_latest_script_version(
            script_id=ws.script_id,
            organization_id=organization_id,
        )
        if script and (best is None or script.version > best.version):
            best = script
    return best


async def _extract_all_blocks_from_main_py(
    script_revision_id: str,
    organization_id: str,
) -> dict[str, str]:
    """Extract all @skyvern.cached block functions from main.py for a script revision.

    Returns {block_label: code} for every block found. Used as a fallback when blocks
    don't have individual block files (e.g. reviewer-created revisions).
    """
    content = await load_main_py_content(script_revision_id, organization_id)
    if not content:
        return {}
    return extract_cached_blocks_from_source(content)


async def _load_cached_script_block_sources(
    script: Script,
    organization_id: str,
) -> dict[str, ScriptBlockSource]:
    """
    Load existing script block sources (code + metadata) for a script revision so they can be reused.

    Blocks may have code stored as individual block files (script_file_id) or only in main.py
    (reviewer-created revisions store code exclusively in main.py). This function tries
    the block file first, then falls back to extracting the block from main.py.
    """
    cached_blocks: dict[str, ScriptBlockSource] = {}

    script_blocks = await app.DATABASE.scripts.get_script_blocks_by_script_revision_id(
        script_revision_id=script.script_revision_id,
        organization_id=organization_id,
    )

    # Lazily loaded: block codes extracted from main.py, used as fallback when
    # a block has no script_file_id (i.e. reviewer-created revisions).
    main_py_block_codes: dict[str, str] | None = None

    for script_block in script_blocks:
        if not script_block.script_block_label:
            continue

        code_str: str | None = None
        if script_block.script_file_id:
            script_file = await app.DATABASE.scripts.get_script_file_by_id(
                script_revision_id=script.script_revision_id,
                file_id=script_block.script_file_id,
                organization_id=organization_id,
            )
            if script_file and script_file.artifact_id:
                artifact = await app.DATABASE.artifacts.get_artifact_by_id(script_file.artifact_id, organization_id)
                if artifact:
                    file_content = await app.ARTIFACT_MANAGER.retrieve_artifact(artifact)
                    if isinstance(file_content, bytes):
                        code_str = file_content.decode("utf-8")
                    elif isinstance(file_content, str):
                        code_str = file_content

        # Fallback: extract block code from main.py (reviewer-created revisions
        # store all block code in main.py and don't create individual block files).
        if not code_str:
            if main_py_block_codes is None:
                main_py_block_codes = await _extract_all_blocks_from_main_py(
                    script_revision_id=script.script_revision_id,
                    organization_id=organization_id,
                )
            code_str = main_py_block_codes.get(script_block.script_block_label)

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
    generation_start = time.monotonic()
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

        adaptive = is_adaptive_caching(workflow, workflow_run)
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

        # Count all descendant blocks inside top-level for-loops (task, extraction,
        # nested for-loops, etc). Does not include blocks inside task_v2 or conditionals.
        forloop_descendant_count = 0
        for blk in codegen_input.workflow_blocks:
            if blk.get("block_type") == "for_loop":
                q: deque[dict[str, Any]] = deque(blk.get("loop_blocks", []))
                while q:
                    inner = q.popleft()
                    forloop_descendant_count += 1
                    if inner.get("block_type") == "for_loop":
                        q.extend(inner.get("loop_blocks", []))

        LOG.info(
            "Script generation block analysis",
            workflow_run_id=workflow_run.workflow_run_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            total_top_level_blocks=len(block_labels),
            forloop_descendant_blocks=forloop_descendant_count,
            cached_blocks=len(cached_block_sources),
            missing_blocks=len(missing_labels),
            blocks_to_regenerate=len(updated_block_labels),
            missing_labels=list(missing_labels),
        )

        codegen_result = await generate_workflow_script_python_code(
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
            use_semantic_selectors=adaptive,
            adaptive_caching=adaptive,
        )
    except Exception:
        generation_duration_ms = (time.monotonic() - generation_start) * 1000
        LOG.error(
            "Failed to generate workflow script source",
            workflow_run_id=workflow_run.workflow_run_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            duration_ms=round(generation_duration_ms, 1),
            exc_info=True,
        )
        return

    python_src = codegen_result.source_code
    blocks_created = codegen_result.blocks_created
    blocks_failed = codegen_result.blocks_failed

    generation_duration_ms = (time.monotonic() - generation_start) * 1000
    LOG.info(
        "Script generation completed",
        workflow_run_id=workflow_run.workflow_run_id,
        workflow_id=workflow.workflow_id,
        workflow_permanent_id=workflow.workflow_permanent_id,
        cache_key_value=rendered_cache_key_value,
        duration_ms=round(generation_duration_ms, 1),
        script_size_bytes=len(python_src.encode("utf-8")),
        blocks_created=blocks_created,
        blocks_failed=blocks_failed,
    )

    if blocks_failed > 0 and blocks_created > 0:
        LOG.warning(
            "Partial script block creation failure — some blocks were not persisted",
            workflow_permanent_id=workflow.workflow_permanent_id,
            script_id=script.script_id,
            blocks_created=blocks_created,
            blocks_failed=blocks_failed,
        )

    # Guard: never persist a zero-block script, regardless of whether the blocks
    # failed or were silently skipped (e.g. generate_script.py:2901 fast-skip for
    # blocks with no actions and no task_id). Publishing an empty revision is the
    # `empty_blocks_detected=True` regression tracked under SKY-8757.
    if blocks_created == 0:
        LOG.error(
            "Script generation produced zero blocks — skipping WorkflowScript creation",
            workflow_permanent_id=workflow.workflow_permanent_id,
            script_id=script.script_id,
            script_revision_id=script.script_revision_id,
            blocks_failed=blocks_failed,
        )
        return

    # 3.5) Post-process: fix static actions inside for-loop blocks
    python_src = _fix_static_actions_in_for_loops(python_src)

    # 3.6) Validate generated Python is syntactically valid.
    # Log a warning but still persist — the Script Reviewer will correct syntax
    # errors when processing the fallback episodes. Returning early here would
    # leave the script revision without files, causing the regeneration path to
    # soft-delete it. That prevents the reviewer from setting
    # new_script_revision_id on episodes, which breaks the Script Update Card
    # on workflow run pages (SKY-8434).
    try:
        compile(python_src, "<generated_script>", "exec")
    except SyntaxError as e:
        LOG.warning(
            "Generated script has syntax error, persisting for Script Reviewer to fix",
            script_id=script.script_id,
            version=script.version,
            error=str(e),
            lineno=e.lineno,
        )

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
        existing_pending_workflow_script = await app.DATABASE.scripts.get_workflow_script(
            organization_id=workflow.organization_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            workflow_run_id=workflow_run.workflow_run_id,
            statuses=[status],
        )
    if not existing_pending_workflow_script:
        # Record the workflow->script mapping for cache lookup
        await app.DATABASE.scripts.create_workflow_script(
            organization_id=workflow.organization_id,
            script_id=script.script_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            cache_key=workflow.cache_key or "",
            cache_key_value=rendered_cache_key_value,
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            status=status,
        )


# ---------------------------------------------------------------------------
# Post-processing: fix static actions inside for-loop blocks
# ---------------------------------------------------------------------------
# Matches `async for current_value in skyvern.loop(...):`  blocks.
_FOR_LOOP_RE = re.compile(
    r"^(?P<indent> *)async for current_value in skyvern\.loop\(.*\):\s*$",
)
# Matches the start of an `await page.click(` call.  We find the balanced
# closing paren programmatically so CSS pseudo-selectors like `:has-text(...)`
# or `:nth-child(2)` don't truncate the match — and we avoid a regex whose
# nested quantifiers would cause exponential backtracking.
_PAGE_CLICK_START_RE = re.compile(r"await page\.click\(")


class _PromptKwargMatch(NamedTuple):
    start: int
    end: int
    quote: str
    value: str


def _find_string_literal_end(text: str, start: int, quote: str) -> int | None:
    """Return the closing quote index for a quoted string, or ``None`` if unterminated."""
    pos = start + 1
    escaped = False

    while pos < len(text):
        ch = text[pos]
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == quote:
            return pos
        pos += 1

    return None


def _is_identifier_char(ch: str) -> bool:
    return ch == "_" or ch.isalnum()


def _find_quoted_prompt_kwarg(text: str) -> tuple[_PromptKwargMatch | None, bool]:
    """Find a quoted ``prompt=...`` kwarg with a deterministic single-pass scan.

    Returns ``(match, False)`` when a quoted prompt kwarg is found, ``(None, False)``
    when no such kwarg exists, and ``(None, True)`` when scanning encounters an
    unterminated string and the call should be left untouched.
    """
    i = 0
    while i < len(text):
        ch = text[i]

        if ch in ("'", '"'):
            string_end = _find_string_literal_end(text, i, ch)
            if string_end is None:
                return None, True
            i = string_end + 1
            continue

        if not text.startswith("prompt", i):
            i += 1
            continue

        prev_char = text[i - 1] if i > 0 else ""
        next_idx = i + len("prompt")
        next_char = text[next_idx] if next_idx < len(text) else ""
        if (prev_char and _is_identifier_char(prev_char)) or (next_char and _is_identifier_char(next_char)):
            i += 1
            continue

        value_start = next_idx
        while value_start < len(text) and text[value_start].isspace():
            value_start += 1
        if value_start >= len(text) or text[value_start] != "=":
            i += 1
            continue

        value_start += 1
        while value_start < len(text) and text[value_start].isspace():
            value_start += 1
        if value_start >= len(text):
            return None, True

        quote = text[value_start]
        if quote not in ("'", '"'):
            i += 1
            continue

        string_end = _find_string_literal_end(text, value_start, quote)
        if string_end is None:
            return None, True

        return (
            _PromptKwargMatch(
                start=i,
                end=string_end + 1,
                quote=quote,
                value=text[value_start + 1 : string_end],
            ),
            False,
        )

    return None, False


def _find_click_calls(text: str) -> list[tuple[int, int, str]]:
    """Find all ``await page.click(...)`` calls with balanced parentheses.

    Returns a list of (start, end, args) tuples where *start*/*end* are byte
    offsets into *text* spanning the full call and *args* is the text between
    the outer parentheses.
    """
    results: list[tuple[int, int, str]] = []
    for m in _PAGE_CLICK_START_RE.finditer(text):
        start = m.start()
        pos = m.end()
        depth = 1
        while pos < len(text) and depth > 0:
            ch = text[pos]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            pos += 1
        if depth == 0:
            results.append((start, pos, text[m.end() : pos - 1]))
    return results


def _fix_static_actions_in_for_loops(code: str) -> str:
    """Detect page.click() calls inside for-loops that don't reference the loop variable.

    When a click action inside a for-loop uses a static selector/prompt (not referencing
    ``current_value``), it will resolve to the same element on every iteration — a common
    bug in generated download scripts.

    Fix: inject ``current_value`` into the prompt and upgrade ``ai='fallback'`` to
    ``ai='proactive'`` so the LLM can disambiguate which element to click.
    """
    lines = code.split("\n")
    result_lines: list[str] = []
    i = 0
    patched_count = 0

    while i < len(lines):
        line = lines[i]
        m = _FOR_LOOP_RE.match(line)
        if not m:
            result_lines.append(line)
            i += 1
            continue

        # Found a for-loop header. Collect the indented body.
        loop_indent = m.group("indent")
        body_indent_prefix = loop_indent + "    "  # 4-space indent inside loop
        result_lines.append(line)
        i += 1

        # Gather body lines (lines that are indented deeper than the for-loop header or blank)
        body_start = len(result_lines)
        while i < len(lines):
            body_line = lines[i]
            # Body continues if the line is blank, or indented deeper than the for-loop
            if body_line.strip() == "" or body_line.startswith(body_indent_prefix):
                result_lines.append(body_line)
                i += 1
            else:
                break

        # Now check the body for static page.click() calls
        body_text = "\n".join(result_lines[body_start:])
        new_body_text = _patch_static_clicks_in_block(body_text)
        if new_body_text != body_text:
            patched_count += 1
            # Replace body lines
            result_lines[body_start:] = new_body_text.split("\n")

    if patched_count > 0:
        LOG.info("Fixed static click actions in for-loop blocks", patched_blocks=patched_count)
    return "\n".join(result_lines)


def _patch_static_clicks_in_block(body: str) -> str:
    """Patch page.click() calls that don't reference current_value."""
    calls = _find_click_calls(body)
    if not calls:
        return body

    # Process matches in reverse order so earlier offsets stay valid.
    for call_start, call_end, args in reversed(calls):
        full = body[call_start:call_end]

        # If the call already references current_value, leave it alone
        if "current_value" in args:
            continue

        # Only patch clicks that explicitly use ai='fallback'.  Proactive
        # clicks already use the LLM, and clicks with no ai= kwarg are not
        # part of the AI-fallback system so should be left alone.
        if "ai='proactive'" in args or 'ai="proactive"' in args:
            continue
        has_fallback = "ai='fallback'" in args or 'ai="fallback"' in args
        if not has_fallback:
            continue

        # Upgrade ai='fallback' to ai='proactive'
        patched = full
        if "ai='fallback'" in patched:
            patched = patched.replace("ai='fallback'", "ai='proactive'")
        elif 'ai="fallback"' in patched:
            patched = patched.replace('ai="fallback"', 'ai="proactive"')

        # Derive indentation from the position of the match in the body text
        # so injected code is correctly aligned regardless of nesting level.
        # Walk backwards from the match start to find the beginning of the line.
        line_start = body.rfind("\n", 0, call_start)
        if line_start == -1:
            leading_text = body[:call_start]
        else:
            leading_text = body[line_start + 1 : call_start]
        base_indent = leading_text if leading_text.isspace() or leading_text == "" else ""
        kwarg_indent = base_indent + "    "

        # Append current_value context to the prompt so the LLM knows which item to target
        # Look for an existing prompt= kwarg and append to it
        prompt_match, malformed_prompt = _find_quoted_prompt_kwarg(patched)
        if malformed_prompt:
            continue
        if prompt_match:
            quote = prompt_match.quote
            original_prompt = prompt_match.value
            # Use an f-string so current_value is evaluated at runtime.
            # Escape existing braces so they are literal in the f-string.
            escaped_prompt = original_prompt.replace("{", "{{").replace("}", "}}")
            new_prompt = f"prompt=f{quote}{escaped_prompt} Target: {{current_value}}{quote}"
            patched = patched[: prompt_match.start] + new_prompt + patched[prompt_match.end :]
        else:
            # No prompt= kwarg — add one with current_value context
            # Insert before the closing paren
            close_paren_idx = patched.rfind(")")
            if close_paren_idx > 0:
                before = patched[:close_paren_idx].rstrip().rstrip(",")
                patched = (
                    before
                    + f",\n{kwarg_indent}prompt=f'Click the element for: {{current_value}}',\n{base_indent}"
                    + patched[close_paren_idx:]
                )

        body = body[:call_start] + patched + body[call_end:]

    return body


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


async def _reconstruct_main_py_from_blocks(
    base_main_py: str,
    updated_blocks: dict[str, str],
) -> str | None:
    """Reconstruct main.py when _patch_main_py fails.

    Takes the header (imports, models, workflow function) from the base main.py,
    then appends each block: updated blocks from the reviewer, plus existing
    blocks extracted from the base main.py.

    Returns the reconstructed source, or None if reconstruction fails.
    """
    try:
        # Extract the header: everything before the first @skyvern.cached decorator
        lines = base_main_py.split("\n")
        header_end = len(lines)
        for i, line in enumerate(lines):
            if _CACHED_DECORATOR_RE.match(line.strip()):
                header_end = i
                break
        # Strip trailing blank lines from header
        while header_end > 0 and not lines[header_end - 1].strip():
            header_end -= 1
        header = "\n".join(lines[:header_end])

        # Extract all existing blocks from the base main.py
        block_codes = extract_cached_blocks_from_source(base_main_py)

        # Override with updated blocks from the reviewer
        for label, code in updated_blocks.items():
            block_codes[label] = code

        if not block_codes:
            LOG.warning("No block code found for reconstruction")
            return None

        # Assemble: header + all blocks with decorators
        parts = [header, ""]
        for label, code in block_codes.items():
            code = code.rstrip("\n")
            # Hoist any leading imports (same as _patch_main_py does)
            imports, body = _split_imports(code)
            if imports:
                # Inject into header (crude but safe — duplicates are harmless)
                for imp in imports:
                    if imp not in header:
                        parts.insert(1, imp)
            # Add the block with its decorator if not already present
            if not body.lstrip().startswith("@skyvern.cached"):
                parts.extend(["", f"@skyvern.cached(cache_key = '{label}')", body])
            else:
                parts.extend(["", body])

        reconstructed = "\n".join(parts) + "\n"
        reconstructed = _fix_static_actions_in_for_loops(reconstructed)

        # Final compile check
        compile(reconstructed, "<reconstructed_main.py>", "exec")
        LOG.info(
            "Successfully reconstructed main.py from base source",
            block_count=len(block_codes),
            block_labels=sorted(block_codes.keys()),
        )
        return reconstructed
    except SyntaxError as exc:
        LOG.error(
            "Reconstructed main.py has syntax error — code assembly issue",
            failure_type="syntax_error",
            error=str(exc),
            lineno=exc.lineno,
        )
        return None
    except Exception as exc:
        LOG.exception(
            "Failed to reconstruct main.py — infrastructure issue (S3/DB)",
            failure_type="infra",
            error=str(exc),
        )
        return None


async def _llm_fix_broken_main_py(
    broken_source: str,
    syntax_error: SyntaxError,
    organization_id: str,
    max_attempts: int = 2,
) -> str | None:
    """Ask an LLM to fix a syntax error in main.py when mechanical reconstruction fails.

    This is a last-resort fix for rare cases where _patch_main_py corrupts the script
    AND reconstruction from block files also fails. The LLM sees the broken source +
    error and makes a targeted syntax fix. Called infrequently, so cost is negligible.

    Returns the fixed source, or None if the LLM can't fix it.
    """
    # Guard against sending very large scripts to the LLM — cap at 50KB
    # (typical scripts are 2-12KB; anything larger suggests an accumulation bug)
    max_source_bytes = 50_000
    if len(broken_source.encode("utf-8")) > max_source_bytes:
        LOG.warning(
            "Skipping LLM fix — script too large",
            source_bytes=len(broken_source.encode("utf-8")),
            max_bytes=max_source_bytes,
        )
        return None

    for attempt in range(1, max_attempts + 1):
        try:
            error_context = (
                f"Line {syntax_error.lineno}: {syntax_error.msg}" if syntax_error.lineno else str(syntax_error)
            )
            prompt = (
                "The following Python script has a syntax error introduced during automated assembly. "
                "Fix ONLY the syntax error. Do not change any logic, selectors, parameters, or function behavior. "
                "Return the complete fixed Python script and nothing else.\n\n"
                f"SYNTAX ERROR: {error_context}\n\n"
                f"BROKEN SCRIPT:\n```python\n{broken_source}\n```"
            )
            response = await app.SCRIPT_REVIEWER_LLM_API_HANDLER(
                prompt=prompt,
                prompt_name="fix-broken-main-py",
                step=None,
                organization_id=organization_id,
            )
            # Extract code from response — log which extraction path fired
            fixed_code: str | None = None
            extraction_path = "none"
            if isinstance(response, str):
                fixed_code = response
                extraction_path = "raw_string"
            elif isinstance(response, dict):
                for key in ("code", "fixed_code", "text"):
                    if response.get(key):
                        fixed_code = response[key]
                        extraction_path = f"dict['{key}']"
                        break
                if not fixed_code:
                    for k, v in response.items():
                        if isinstance(v, str) and "async def " in v:
                            fixed_code = v
                            extraction_path = f"dict_scan['{k}']"
                            break
            if not fixed_code:
                LOG.warning("LLM fix: no code in response", attempt=attempt, extraction_path=extraction_path)
                continue
            LOG.info("LLM fix: extracted code", attempt=attempt, extraction_path=extraction_path)

            # Strip markdown code fences if present
            if "```python" in fixed_code:
                fixed_code = fixed_code.split("```python", 1)[1]
                if "```" in fixed_code:
                    fixed_code = fixed_code.split("```", 1)[0]
            elif "```" in fixed_code:
                fixed_code = fixed_code.split("```", 1)[1]
                if "```" in fixed_code:
                    fixed_code = fixed_code.split("```", 1)[0]
            fixed_code = fixed_code.strip()

            # Verify the fix compiles
            compile(fixed_code, "<llm-fixed-main.py>", "exec")
            LOG.info(
                "LLM successfully fixed broken main.py",
                attempt=attempt,
                original_error=error_context,
            )
            return fixed_code
        except SyntaxError as new_err:
            LOG.warning(
                "LLM fix still has syntax error",
                attempt=attempt,
                error=str(new_err),
            )
            # Update the error for the next attempt
            syntax_error = new_err
        except Exception:
            LOG.exception("LLM fix attempt failed", attempt=attempt)
    return None


async def _find_main_py_content(script_id: str, organization_id: str, base_revision_id: str) -> str | None:
    """Find main.py content, preferring the base revision then falling back to v1.

    After this fix is deployed, every new version will have a patched main.py, so
    the base revision will always have one. The v1 fallback exists for bootstrapping
    (versions created before this fix existed).
    """

    async def _load_main_py_from_revision(revision_id: str) -> str | None:
        files = await app.DATABASE.scripts.get_script_files(
            script_revision_id=revision_id,
            organization_id=organization_id,
        )
        for f in files:
            if f.file_path == "main.py" and f.artifact_id:
                artifact = await app.DATABASE.artifacts.get_artifact_by_id(f.artifact_id, organization_id)
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
    v1_script = await app.DATABASE.scripts.get_script(
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
    workflow_run: WorkflowRun | None = None,
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
        # Defense-in-depth: refuse to create a correction for a pinned script.
        # _trigger_script_reviewer() already gates on is_script_pinned(), but
        # that check can be bypassed when the skyvern context is missing.
        # Guard here so no code path can mutate a pinned script.
        if await app.DATABASE.scripts.is_script_pinned(
            organization_id=organization_id,
            script_id=base_script.script_id,
        ):
            LOG.info(
                "Skipping script correction — script is pinned",
                organization_id=organization_id,
                script_id=base_script.script_id,
                workflow_permanent_id=workflow_permanent_id,
            )
            return None

        # Create a new script version
        new_script = await app.DATABASE.scripts.create_script(
            organization_id=organization_id,
            script_id=base_script.script_id,
            version=base_script.version + 1,
            run_id=workflow_run.workflow_run_id if workflow_run else None,
        )

        # Copy existing script blocks from the base revision
        existing_blocks = await app.DATABASE.scripts.get_script_blocks_by_script_revision_id(
            script_revision_id=base_script.script_revision_id,
            organization_id=organization_id,
        )

        conditional_blocks = conditional_blocks or {}

        for sb in existing_blocks:
            # Determine if this is a conditional block being upgraded to code.
            is_conditional_upgrade = sb.script_block_label in conditional_blocks
            block_requires_agent = False if is_conditional_upgrade else sb.requires_agent
            block_run_signature = (
                f'await skyvern.conditional(label="{sb.script_block_label}")'
                if is_conditional_upgrade
                else sb.run_signature
            )

            # Create script block entry (metadata only — code lives in main.py).
            # All blocks in this version are attributed to the triggering run — even
            # non-updated blocks — so the version has uniform provenance.
            await app.DATABASE.scripts.create_script_block(
                organization_id=organization_id,
                script_id=new_script.script_id,
                script_revision_id=new_script.script_revision_id,
                script_block_label=sb.script_block_label,
                run_signature=block_run_signature,
                workflow_run_id=workflow_run.workflow_run_id if workflow_run else sb.workflow_run_id,
                workflow_run_block_id=sb.workflow_run_block_id,
                input_fields=sb.input_fields,
                requires_agent=block_requires_agent,
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
            patched_main = _fix_static_actions_in_for_loops(patched_main)

            # Validate the patched main.py compiles. If the splice corrupted it,
            # reconstruct from the base main.py source instead of persisting
            # a broken script.
            try:
                compile(patched_main, "<patched_main.py>", "exec")
            except SyntaxError as exc:
                LOG.warning(
                    "Patched main.py has syntax error — reconstructing from base source",
                    script_id=new_script.script_id,
                    error=str(exc),
                    lineno=exc.lineno,
                    organization_id=organization_id,
                )
                reconstructed = await _reconstruct_main_py_from_blocks(
                    base_main_py=main_py_content,
                    updated_blocks=updated_blocks,
                )
                if reconstructed:
                    patched_main = reconstructed
                else:
                    # Reconstruction failed too — ask an LLM to fix the syntax error.
                    # This is rare and cheap; the LLM sees the full script + error.
                    llm_fixed = await _llm_fix_broken_main_py(
                        broken_source=patched_main,
                        syntax_error=exc,
                        organization_id=organization_id,
                    )
                    if llm_fixed:
                        patched_main = llm_fixed
                    else:
                        # All recovery attempts failed. Delete the workflow script
                        # mapping so the next run falls back to AI and regenerates
                        # a completely fresh script.
                        LOG.error(
                            "All main.py recovery attempts failed — deleting workflow script mapping",
                            script_id=new_script.script_id,
                            organization_id=organization_id,
                            workflow_permanent_id=workflow_permanent_id,
                        )
                        await app.DATABASE.scripts.delete_workflow_scripts_by_permanent_id(
                            organization_id=organization_id,
                            workflow_permanent_id=workflow_permanent_id,
                        )
                        return new_script

            patched_bytes = patched_main.encode("utf-8")
            patched_hash = hashlib.sha256(patched_bytes).hexdigest()

            main_artifact_id = await app.ARTIFACT_MANAGER.create_script_file_artifact(
                organization_id=organization_id,
                script_id=new_script.script_id,
                script_version=new_script.version,
                file_path="main.py",
                data=patched_bytes,
            )
            await app.DATABASE.scripts.create_script_file(
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
            source_files = await app.DATABASE.scripts.get_script_files(
                script_revision_id=base_script.script_revision_id,
                organization_id=organization_id,
            )
            if not any(f.file_path != "main.py" and not f.file_path.startswith("blocks/") for f in source_files):
                # Base revision has no non-block files, fall back to v1
                v1_script = await app.DATABASE.scripts.get_script(
                    script_id=base_script.script_id,
                    organization_id=organization_id,
                    version=1,
                )
                if v1_script:
                    source_files = await app.DATABASE.scripts.get_script_files(
                        script_revision_id=v1_script.script_revision_id,
                        organization_id=organization_id,
                    )

            for f in source_files:
                # Skip main.py (already patched) and any legacy block files
                if f.file_path == "main.py" or f.file_path.startswith("blocks/"):
                    continue
                await app.DATABASE.scripts.create_script_file(
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
        if workflow_run:
            _script, rendered_cache_key_value, _is_pinned = await get_workflow_script(
                workflow=workflow,
                workflow_run=workflow_run,
                status=ScriptStatus.published,
            )
        else:
            # No workflow run — look up the existing cache key value from the base script
            existing_ws = await app.DATABASE.scripts.get_workflow_scripts_by_permanent_id(
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
                statuses=[ScriptStatus.published],
            )
            rendered_cache_key_value = ""
            for ws in existing_ws:
                if ws.script_id == base_script.script_id:
                    rendered_cache_key_value = ws.cache_key_value
                    break

        await app.DATABASE.scripts.create_workflow_script(
            organization_id=organization_id,
            script_id=new_script.script_id,
            workflow_permanent_id=workflow_permanent_id,
            cache_key=workflow.cache_key or "",
            cache_key_value=rendered_cache_key_value,
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id if workflow_run else None,
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
