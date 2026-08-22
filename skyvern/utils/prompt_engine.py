from collections.abc import Callable
from typing import Any

import structlog
from pydantic import BaseModel

from skyvern.constants import DEFAULT_MAX_TOKENS
from skyvern.errors.errors import UserDefinedError
from skyvern.exceptions import SkyvernContextWindowExceededError
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.prompting import PromptEngine
from skyvern.utils.strings import escape_code_fences
from skyvern.utils.token_counter import count_tokens
from skyvern.webeye.scraper.scraped_page import ElementTreeBuilder

LOG = structlog.get_logger()


def _sanitize_elements_for_prompt(builder: ElementTreeBuilder, html: str) -> str:
    # Mirror the sanitized form onto last_used_element_tree_html so the
    # extraction cache key (which hashes that field) matches what the LLM saw.
    sanitized = escape_code_fences(html)
    if builder.last_used_element_tree_html is not None:
        builder.last_used_element_tree_html = sanitized
    return sanitized


class CheckPhoneNumberFormatResponse(BaseModel):
    page_info: str
    is_phone_number_input: bool
    thought: str
    phone_number_format: str | None
    is_current_format_correct: bool | None
    recommended_phone_number: str | None


class CheckDateFormatResponse(BaseModel):
    page_info: str
    thought: str
    is_current_format_correct: bool
    recommended_date: str | None


HTMLTreeStr = str


class MaxStepsReasonResponse(BaseModel):
    page_info: str
    reasoning: str
    errors: list[UserDefinedError] = []
    failure_categories: list[dict] = []
    # Explicit provenance for failure_categories. Set by short-circuit paths
    # to avoid the caller inferring "llm" from the presence of categories.
    failure_category_source: str | None = None


PROMPT_HARD_CEILING_TOKENS = 180_000
_CEILING_SAFETY_MARGIN_TOKENS = 2_000
_MIN_USEFUL_ELEMENT_TOKENS = 20_000
_MAX_ELEMENT_TRIM_ROUNDS = 3

CEILING_FALLBACK_KEYS_BY_TEMPLATE: dict[str, list[str]] = {
    "extract-information": [
        "virtualized_grid_rows",
        "previous_extracted_information",
        "extracted_information_schema",
        "extracted_text",
    ],
    "extract-action": ["action_history", "navigation_payload_str"],
    "extract-action-dynamic": ["action_history", "navigation_payload_str"],
    "extract-action-static": [],
    "data-extraction-summary": ["data_extraction_schema"],
}


def load_prompt_with_elements_tracked(
    element_tree_builder: ElementTreeBuilder,
    prompt_engine: PromptEngine,
    template_name: str,
    html_need_skyvern_attrs: bool = True,
    *,
    # SKY-9718 Layer 1 — deterministic lean-tree transforms. Each flag toggles
    # one transform independently. Callers decide which to enable (typically by
    # AND-ing with `skyvern_context.current().enable_lean_element_tree` if they
    # want the experiment gate). To drop Skyvern internal IDs from the rendered
    # HTML, callers pass `html_need_skyvern_attrs=False` — that's the existing
    # `json_to_html` mechanism and stacks on top of any lean flags chosen here.
    lean_compress_long_href: bool = False,
    lean_compress_image_src: bool = False,
    lean_strip_url_query_strings: bool = False,
    lean_compress_nonnavigable_href: bool = False,
    **kwargs: Any,
) -> tuple[str, dict[str, Any]]:
    """Same as load_prompt_with_elements but also returns post-ceiling kwargs.

    The returned kwargs dict reflects every fallback key that was set to None
    to bring the prompt under the hard ceiling. Callers that hash prompt
    inputs for caching should use these values instead of the pre-drop kwargs
    so two requests that render to the same final prompt share a cache key.
    """
    lean_any = (
        lean_compress_long_href
        or lean_compress_image_src
        or lean_strip_url_query_strings
        or lean_compress_nonnavigable_href
    )
    if lean_any and element_tree_builder.support_lean_elements_tree():
        elements = _sanitize_elements_for_prompt(
            element_tree_builder,
            element_tree_builder.build_lean_elements_tree(
                html_need_skyvern_attrs=html_need_skyvern_attrs,
                compress_long_href=lean_compress_long_href,
                compress_image_src=lean_compress_image_src,
                strip_url_query_strings=lean_strip_url_query_strings,
                compress_nonnavigable_href=lean_compress_nonnavigable_href,
            ),
        )
    else:
        # Builder doesn't implement lean (e.g. IncrementalScrapePage) or caller
        # asked for no transforms — fall back to the plain element tree.
        elements = _sanitize_elements_for_prompt(
            element_tree_builder,
            element_tree_builder.build_element_tree(html_need_skyvern_attrs=html_need_skyvern_attrs),
        )
    prompt = prompt_engine.load_prompt(
        template_name,
        elements=elements,
        **kwargs,
    )
    token_count = count_tokens(prompt)
    # Invariant: equals count_tokens(prompt) for the current prompt; re-set on every rebuild
    # below so the ceiling helper and telemetry can reuse it without re-encoding an identical string.
    current_prompt_token_count = token_count
    if token_count > DEFAULT_MAX_TOKENS and element_tree_builder.support_economy_elements_tree():
        # get rid of all the secondary elements like SVG, etc
        # NOTE: economy fallback drops the lean recipe — context-overflow firefighting
        # path; we accept the lean savings loss in exchange for fitting under the cap.
        elements = _sanitize_elements_for_prompt(
            element_tree_builder,
            element_tree_builder.build_economy_elements_tree(html_need_skyvern_attrs=html_need_skyvern_attrs),
        )
        prompt = prompt_engine.load_prompt(template_name, elements=elements, **kwargs)
        economy_token_count = count_tokens(prompt)
        current_prompt_token_count = economy_token_count
        LOG.warning(
            "Prompt is longer than the max tokens. Going to use the economy elements tree.",
            template_name=template_name,
            token_count=token_count,
            economy_token_count=economy_token_count,
            max_tokens=DEFAULT_MAX_TOKENS,
        )
        if economy_token_count > DEFAULT_MAX_TOKENS:
            # !!! HACK alert
            # dump the last 1/3 of the html context and keep the first 2/3 of the html context
            elements = _sanitize_elements_for_prompt(
                element_tree_builder,
                element_tree_builder.build_economy_elements_tree(
                    html_need_skyvern_attrs=html_need_skyvern_attrs,
                    percent_to_keep=2 / 3,
                ),
            )
            prompt = prompt_engine.load_prompt(template_name, elements=elements, **kwargs)
            token_count_after_dump = count_tokens(prompt)
            current_prompt_token_count = token_count_after_dump
            LOG.warning(
                "Prompt is still longer than the max tokens. Will only keep the first 2/3 of the html context.",
                template_name=template_name,
                token_count=token_count,
                economy_token_count=economy_token_count,
                token_count_after_dump=token_count_after_dump,
                max_tokens=DEFAULT_MAX_TOKENS,
            )

    def _mirror_trimmed_elements(trimmed: str) -> None:
        if element_tree_builder.last_used_element_tree_html is not None:
            element_tree_builder.last_used_element_tree_html = trimmed

    final_prompt, final_kwargs, final_token_count = _enforce_prompt_ceiling_counted(
        prompt,
        prompt_engine=prompt_engine,
        template_name=template_name,
        kwargs=kwargs,
        elements=elements,
        precomputed_token_count=current_prompt_token_count,
        on_elements_trimmed=_mirror_trimmed_elements,
    )

    # SKY-9718: stash the locally-counted prompt size on SkyvernContext so the downstream
    # LLM API handler log can attach it alongside the provider's input_tokens / llm_cost.
    ctx = skyvern_context.current()
    if ctx is not None:
        ctx.last_prompt_breakdown = {
            "total_tokens_local": final_token_count,
            "template_name": template_name,
        }

    return final_prompt, final_kwargs


def load_prompt_with_elements(
    element_tree_builder: ElementTreeBuilder,
    prompt_engine: PromptEngine,
    template_name: str,
    html_need_skyvern_attrs: bool = True,
    *,
    lean_compress_long_href: bool = False,
    lean_compress_image_src: bool = False,
    lean_strip_url_query_strings: bool = False,
    lean_compress_nonnavigable_href: bool = False,
    **kwargs: Any,
) -> str:
    prompt, _ = load_prompt_with_elements_tracked(
        element_tree_builder=element_tree_builder,
        prompt_engine=prompt_engine,
        template_name=template_name,
        html_need_skyvern_attrs=html_need_skyvern_attrs,
        lean_compress_long_href=lean_compress_long_href,
        lean_compress_image_src=lean_compress_image_src,
        lean_strip_url_query_strings=lean_strip_url_query_strings,
        lean_compress_nonnavigable_href=lean_compress_nonnavigable_href,
        **kwargs,
    )
    return prompt


def _truncate_elements_at_tag_boundary(elements: str, keep_chars: int) -> str:
    if keep_chars >= len(elements):
        return elements
    # Cut on a tag boundary so the tail isn't a half-written tag or attribute.
    last_tag_end = elements.rfind(">", 0, keep_chars)
    return elements[: last_tag_end + 1] if last_tag_end != -1 else elements[:keep_chars]


def _trim_elements_to_fit(
    prompt: str,
    *,
    prompt_engine: PromptEngine,
    template_name: str,
    kwargs: dict[str, Any],
    elements: str,
    token_count: int,
    min_useful_element_tokens: int,
) -> tuple[str, str, int]:
    """Trim the rendered element tree until the prompt fits under the hard ceiling.

    Returns the (possibly unchanged) prompt, elements, and token count. The tree is left
    alone when the non-element inputs are what blow the budget — trimming it then costs
    page context without getting the prompt under the ceiling.
    """
    target_tokens = PROMPT_HARD_CEILING_TOKENS - _CEILING_SAFETY_MARGIN_TOKENS
    trimmed = elements
    for _ in range(_MAX_ELEMENT_TRIM_ROUNDS):
        element_tokens = count_tokens(trimmed)
        if element_tokens == 0:
            break
        element_budget = target_tokens - (token_count - element_tokens)
        if element_budget < min_useful_element_tokens:
            break
        keep_chars = int(element_budget * (len(trimmed) / element_tokens))
        if keep_chars >= len(trimmed):
            break
        trimmed = _truncate_elements_at_tag_boundary(trimmed, keep_chars)
        prompt = prompt_engine.load_prompt(template_name, elements=trimmed, **kwargs)
        token_count = count_tokens(prompt)
        if token_count <= PROMPT_HARD_CEILING_TOKENS:
            break
    return prompt, trimmed, token_count


def _enforce_prompt_ceiling_counted(
    prompt: str,
    *,
    prompt_engine: PromptEngine,
    template_name: str,
    kwargs: dict[str, Any],
    elements: Any | None = None,
    precomputed_token_count: int | None = None,
    on_elements_trimmed: Callable[[str], None] | None = None,
) -> tuple[str, dict[str, Any], int]:
    """Count-aware core of the hard-ceiling enforcement, returning the final prompt's token count.

    ``precomputed_token_count`` is trusted only for the first ceiling check and must be the count
    of exactly this ``prompt``. Any prompt mutation inside the drop loop is re-encoded — a count is
    never reused across a changed prompt.
    """
    working_kwargs = dict(kwargs)
    final_token_count = precomputed_token_count if precomputed_token_count is not None else count_tokens(prompt)
    if final_token_count <= PROMPT_HARD_CEILING_TOKENS:
        return prompt, working_kwargs, final_token_count

    original_elements: str | None = elements if isinstance(elements, str) else None
    # Mirrors the `elements` parameter so the pass-through to load_prompt is unchanged for
    # callers that render without one; only a non-empty rendered tree is ever trimmed.
    working_elements: Any | None = elements

    def _fitted() -> tuple[str, dict[str, Any], int]:
        if (
            original_elements is not None
            and isinstance(working_elements, str)
            and working_elements != original_elements
        ):
            LOG.warning(
                "Prompt exceeded hard ceiling; trimmed the element tree to fit",
                template_name=template_name,
                elements_char_count_before=len(original_elements),
                elements_char_count_after=len(working_elements),
                final_token_count=final_token_count,
                hard_ceiling=PROMPT_HARD_CEILING_TOKENS,
            )
            if on_elements_trimmed is not None:
                on_elements_trimmed(working_elements)
        return prompt, working_kwargs, final_token_count

    # The element tree is usually what blows the ceiling, and no combination of fallback
    # drops can rescue a prompt whose tree alone is over budget. Trim it first so those
    # drops aren't spent for nothing; `_MIN_USEFUL_ELEMENT_TOKENS` keeps this from gutting
    # the page when a large non-element input is the real cause.
    if isinstance(working_elements, str) and working_elements:
        prompt, working_elements, final_token_count = _trim_elements_to_fit(
            prompt,
            prompt_engine=prompt_engine,
            template_name=template_name,
            kwargs=working_kwargs,
            elements=working_elements,
            token_count=final_token_count,
            min_useful_element_tokens=_MIN_USEFUL_ELEMENT_TOKENS,
        )
        if final_token_count <= PROMPT_HARD_CEILING_TOKENS:
            return _fitted()

    fallback_keys = CEILING_FALLBACK_KEYS_BY_TEMPLATE.get(template_name, [])
    drops_applied = 0
    for drop_key in fallback_keys:
        if working_kwargs.get(drop_key) is None:
            continue
        LOG.warning(
            "Prompt exceeds hard ceiling; dropping fallback key",
            template_name=template_name,
            drop_key=drop_key,
            final_token_count=final_token_count,
            hard_ceiling=PROMPT_HARD_CEILING_TOKENS,
        )
        working_kwargs[drop_key] = None
        drops_applied += 1
        if working_elements is None:
            prompt = prompt_engine.load_prompt(template_name, **working_kwargs)
        else:
            prompt = prompt_engine.load_prompt(template_name, elements=working_elements, **working_kwargs)
        final_token_count = count_tokens(prompt)
        if final_token_count <= PROMPT_HARD_CEILING_TOKENS:
            return _fitted()

    # Last resort: the drops freed whatever they could, so trim the tree as far as it takes.
    # A degraded view of the page still lets the step run; raising ends the run outright.
    if isinstance(working_elements, str) and working_elements:
        prompt, working_elements, final_token_count = _trim_elements_to_fit(
            prompt,
            prompt_engine=prompt_engine,
            template_name=template_name,
            kwargs=working_kwargs,
            elements=working_elements,
            token_count=final_token_count,
            min_useful_element_tokens=0,
        )
        if final_token_count <= PROMPT_HARD_CEILING_TOKENS:
            return _fitted()

    LOG.error(
        "Prompt still exceeds hard ceiling",
        template_name=template_name,
        final_token_count=final_token_count,
        hard_ceiling=PROMPT_HARD_CEILING_TOKENS,
        fallback_keys_configured=len(fallback_keys),
        drops_applied=drops_applied,
        elements_char_count=len(original_elements) if original_elements else None,
        elements_char_count_after_trim=len(working_elements) if working_elements else None,
    )
    raise SkyvernContextWindowExceededError(prompt_name=template_name)


def enforce_prompt_ceiling_tracked(
    prompt: str,
    *,
    prompt_engine: PromptEngine,
    template_name: str,
    kwargs: dict[str, Any],
    elements: Any | None = None,
) -> tuple[str, dict[str, Any]]:
    """Same as enforce_prompt_ceiling but also returns post-drop kwargs.

    Callers that derive a cache key from the prompt inputs should hash the
    returned kwargs so requests that render to the same final LLM prompt
    (because dropped fields differed but were both dropped) share a key.
    """
    final_prompt, working_kwargs, _ = _enforce_prompt_ceiling_counted(
        prompt,
        prompt_engine=prompt_engine,
        template_name=template_name,
        kwargs=kwargs,
        elements=elements,
    )
    return final_prompt, working_kwargs


def enforce_prompt_ceiling(
    prompt: str,
    *,
    prompt_engine: PromptEngine,
    template_name: str,
    kwargs: dict[str, Any],
    elements: Any | None = None,
) -> str:
    """Drop fallback-chain keys in priority order until the prompt fits.

    Use this at any call site that builds a prompt via prompt_engine.load_prompt
    directly, so the 180k hard ceiling is enforced regardless of whether the
    caller went through load_prompt_with_elements.
    """
    prompt, _ = enforce_prompt_ceiling_tracked(
        prompt,
        prompt_engine=prompt_engine,
        template_name=template_name,
        kwargs=kwargs,
        elements=elements,
    )
    return prompt
