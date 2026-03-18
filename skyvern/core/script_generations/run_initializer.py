import hashlib
from typing import Any

import structlog
from cachetools import TTLCache
from pydantic import BaseModel

from skyvern.core.script_generations.canonical_fields import CANONICAL_CATEGORIES
from skyvern.core.script_generations.script_skyvern_page import ScriptSkyvernPage, script_run_context_manager
from skyvern.core.script_generations.skyvern_page import RunContext, SkyvernPage
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameterType

LOG = structlog.get_logger()

# Cache for LLM-extracted canonical values. Key: hash of (text_blobs + category names).
# TTL of 1 hour, max 256 entries. Identical inputs across runs skip the LLM call.
_extraction_cache: TTLCache[str, dict[str, str | None]] = TTLCache(maxsize=256, ttl=3600)


async def _extract_canonical_values(parameters: dict[str, Any]) -> dict[str, str | None]:
    """Extract canonical field values from ALL parameters — no magic names.

    Strategy:
    1. Direct mapping: if a parameter key matches a canonical category's param
       name (e.g., "email" → email category), use the value directly. No LLM needed.
    2. LLM extraction: for long text blobs (>100 chars), run a single LLM call
       to extract remaining canonical values.

    Returns a dict keyed by canonical category name -> extracted value (or None).
    """
    result: dict[str, str | None] = {}
    param_to_category = {c.param: c.name for c in CANONICAL_CATEGORIES if c.param}
    text_blobs: list[str] = []

    for key, value in parameters.items():
        if not isinstance(value, str) or not value:
            continue

        # Direct mapping: parameter key matches a canonical category's param
        if key in param_to_category:
            result[param_to_category[key]] = value
            continue

        # Collect long text values for LLM extraction
        if len(value) > 100:
            text_blobs.append(value)

    # LLM extraction for remaining categories
    if text_blobs:
        remaining_cats = [c for c in CANONICAL_CATEGORIES if c.name not in result]
        if remaining_cats:
            combined_text = "\n\n".join(text_blobs)
            llm_result = await _llm_extract_from_text(combined_text, remaining_cats)
            result.update(llm_result)

    LOG.info(
        "extract_canonical_values: complete",
        direct_count=sum(1 for k, v in result.items() if v is not None and k in param_to_category.values()),
        llm_count=sum(1 for v in result.values() if v is not None)
        - sum(1 for k, v in result.items() if v is not None and k in param_to_category.values()),
        total_count=sum(1 for v in result.values() if v is not None),
    )
    return result


async def _llm_extract_from_text(
    input_text: str,
    categories: list[Any],
) -> dict[str, str | None]:
    """Extract canonical field values from a text blob using a single LLM call.

    Results are cached by hash of (input_text + category names) so identical
    inputs across runs skip the LLM call entirely.

    Returns a dict keyed by canonical category name -> extracted value (or None).
    """
    # Build cache key from input text + sorted category names
    cat_names_str = ",".join(sorted(c.name for c in categories))
    cache_key = hashlib.sha256(f"{input_text}|{cat_names_str}".encode()).hexdigest()

    cached = _extraction_cache.get(cache_key)
    if cached is not None:
        LOG.info("llm_extract_from_text: cache hit, skipping LLM call", cache_key=cache_key[:12])
        return cached

    cat_descs = [{"name": c.name, "prompt": c.prompt} for c in categories]

    prompt_text = prompt_engine.load_prompt(
        template="extract-applicant-parameters",
        input_text=input_text,
        categories=cat_descs,
    )

    try:
        skyvern_ctx = skyvern_context.current()
        org_id = skyvern_ctx.organization_id if skyvern_ctx else None
        result = await app.SECONDARY_LLM_API_HANDLER(
            prompt=prompt_text,
            prompt_name="extract-applicant-parameters",
            organization_id=org_id,
        )
        if isinstance(result, dict):
            valid_names = {c.name for c in categories}
            filtered = {k: (str(v) if v is not None else None) for k, v in result.items() if k in valid_names}
            LOG.info("llm_extract_from_text: extracted values", count=sum(1 for v in filtered.values() if v))
            _extraction_cache[cache_key] = filtered
            return filtered
    except Exception as e:
        LOG.warning("llm_extract_from_text: LLM call failed, skipping", error_type=type(e).__name__, exc_info=True)

    return {}


async def setup(
    parameters: dict[str, Any],
    generated_parameter_cls: type[BaseModel] | None = None,
    browser_session_id: str | None = None,
    adaptive_caching: bool = False,
) -> tuple[SkyvernPage, RunContext]:
    # transform any secrets/credential parameters. For example, if there's only one credential in the parameters: {"cred_12345": "cred_12345"},
    # it should be transformed to {"cred_12345": {"username": "secret_5fBoa_username", "password": "secret_5fBoa_password"}}
    # context comes from app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(workflow_run_id)
    context = skyvern_context.current()
    if context and context.organization_id and context.workflow_run_id:
        browser_session_id = browser_session_id or context.browser_session_id
        workflow_run_context = app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context(context.workflow_run_id)
        parameters_in_workflow_context = workflow_run_context.parameters
        for key in parameters:
            if key in parameters_in_workflow_context:
                parameter = parameters_in_workflow_context[key]
                if parameter.workflow_parameter_type == WorkflowParameterType.CREDENTIAL_ID:
                    parameters[key] = workflow_run_context.values[key]
        context.script_run_parameters.update(parameters)

    # Pre-extract structured values from all parameters (1 LLM call max, cached for entire run)
    if adaptive_caching:
        extracted_params = await _extract_canonical_values(parameters)
    else:
        extracted_params = {}

    skyvern_page = await ScriptSkyvernPage.create(browser_session_id=browser_session_id)
    run_context = RunContext(
        parameters=parameters,
        page=skyvern_page,
        generated_parameters=generated_parameter_cls().model_dump() if generated_parameter_cls else None,
        extracted_params=extracted_params,
    )
    script_run_context_manager.set_run_context(run_context)
    return skyvern_page, run_context
