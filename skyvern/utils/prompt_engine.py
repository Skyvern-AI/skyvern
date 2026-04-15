from typing import Any

import structlog
from pydantic import BaseModel

from skyvern.constants import DEFAULT_MAX_TOKENS
from skyvern.errors.errors import UserDefinedError
from skyvern.forge.sdk.prompting import PromptEngine
from skyvern.utils.token_counter import count_tokens
from skyvern.webeye.scraper.scraped_page import ElementTreeBuilder

LOG = structlog.get_logger()


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


PROMPT_HARD_CEILING_TOKENS = 180_000

CEILING_FALLBACK_KEYS_BY_TEMPLATE: dict[str, list[str]] = {
    "extract-information": [
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
    **kwargs: Any,
) -> tuple[str, dict[str, Any]]:
    """Same as load_prompt_with_elements but also returns post-ceiling kwargs.

    The returned kwargs dict reflects every fallback key that was set to None
    to bring the prompt under the hard ceiling. Callers that hash prompt
    inputs for caching should use these values instead of the pre-drop kwargs
    so two requests that render to the same final prompt share a cache key.
    """
    elements = element_tree_builder.build_element_tree(html_need_skyvern_attrs=html_need_skyvern_attrs)
    prompt = prompt_engine.load_prompt(
        template_name,
        elements=elements,
        **kwargs,
    )
    token_count = count_tokens(prompt)
    if token_count > DEFAULT_MAX_TOKENS and element_tree_builder.support_economy_elements_tree():
        # get rid of all the secondary elements like SVG, etc
        elements = element_tree_builder.build_economy_elements_tree(html_need_skyvern_attrs=html_need_skyvern_attrs)
        prompt = prompt_engine.load_prompt(template_name, elements=elements, **kwargs)
        economy_token_count = count_tokens(prompt)
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
            elements = element_tree_builder.build_economy_elements_tree(
                html_need_skyvern_attrs=html_need_skyvern_attrs,
                percent_to_keep=2 / 3,
            )
            prompt = prompt_engine.load_prompt(template_name, elements=elements, **kwargs)
            token_count_after_dump = count_tokens(prompt)
            LOG.warning(
                "Prompt is still longer than the max tokens. Will only keep the first 2/3 of the html context.",
                template_name=template_name,
                token_count=token_count,
                economy_token_count=economy_token_count,
                token_count_after_dump=token_count_after_dump,
                max_tokens=DEFAULT_MAX_TOKENS,
            )

    return enforce_prompt_ceiling_tracked(
        prompt,
        prompt_engine=prompt_engine,
        template_name=template_name,
        kwargs=kwargs,
        elements=elements,
    )


def load_prompt_with_elements(
    element_tree_builder: ElementTreeBuilder,
    prompt_engine: PromptEngine,
    template_name: str,
    html_need_skyvern_attrs: bool = True,
    **kwargs: Any,
) -> str:
    prompt, _ = load_prompt_with_elements_tracked(
        element_tree_builder=element_tree_builder,
        prompt_engine=prompt_engine,
        template_name=template_name,
        html_need_skyvern_attrs=html_need_skyvern_attrs,
        **kwargs,
    )
    return prompt


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
    working_kwargs = dict(kwargs)
    final_token_count = count_tokens(prompt)
    if final_token_count <= PROMPT_HARD_CEILING_TOKENS:
        return prompt, working_kwargs
    fallback_keys = CEILING_FALLBACK_KEYS_BY_TEMPLATE.get(template_name, [])
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
        if elements is None:
            prompt = prompt_engine.load_prompt(template_name, **working_kwargs)
        else:
            prompt = prompt_engine.load_prompt(template_name, elements=elements, **working_kwargs)
        final_token_count = count_tokens(prompt)
        if final_token_count <= PROMPT_HARD_CEILING_TOKENS:
            return prompt, working_kwargs
    LOG.error(
        "Prompt still exceeds hard ceiling after all fallback drops",
        template_name=template_name,
        final_token_count=final_token_count,
        hard_ceiling=PROMPT_HARD_CEILING_TOKENS,
    )
    return prompt, working_kwargs


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
