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
    errors: list[UserDefinedError]


def load_prompt_with_elements(
    element_tree_builder: ElementTreeBuilder,
    prompt_engine: PromptEngine,
    template_name: str,
    html_need_skyvern_attrs: bool = True,
    **kwargs: Any,
) -> str:
    elements = element_tree_builder.build_element_tree(html_need_skyvern_attrs=html_need_skyvern_attrs)
    prompt = prompt_engine.load_prompt(
        template_name,
        elements=elements,
        **kwargs,
    )
    token_count = count_tokens(prompt)
    if token_count > DEFAULT_MAX_TOKENS and element_tree_builder.support_economy_elements_tree():
        # get rid of all the secondary elements like SVG, etc
        economy_elements_tree = element_tree_builder.build_economy_elements_tree(
            html_need_skyvern_attrs=html_need_skyvern_attrs
        )
        prompt = prompt_engine.load_prompt(template_name, elements=economy_elements_tree, **kwargs)
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
            economy_elements_tree_dumped = element_tree_builder.build_economy_elements_tree(
                html_need_skyvern_attrs=html_need_skyvern_attrs,
                percent_to_keep=2 / 3,
            )
            prompt = prompt_engine.load_prompt(template_name, elements=economy_elements_tree_dumped, **kwargs)
            token_count_after_dump = count_tokens(prompt)
            LOG.warning(
                "Prompt is still longer than the max tokens. Will only keep the first 2/3 of the html context.",
                template_name=template_name,
                token_count=token_count,
                economy_token_count=economy_token_count,
                token_count_after_dump=token_count_after_dump,
                max_tokens=DEFAULT_MAX_TOKENS,
            )
    return prompt
