from typing import Any

import structlog

from skyvern.forge.sdk.prompting import PromptEngine
from skyvern.utils.token_counter import count_tokens
from skyvern.webeye.scraper.scraper import ScrapedPage

DEFAULT_MAX_TOKENS = 100000
LOG = structlog.get_logger()


def load_prompt_with_elements(
    scraped_page: ScrapedPage,
    prompt_engine: PromptEngine,
    template_name: str,
    **kwargs: Any,
) -> str:
    prompt = prompt_engine.load_prompt(template_name, elements=scraped_page.build_element_tree(), **kwargs)
    token_count = count_tokens(prompt)
    if token_count > DEFAULT_MAX_TOKENS:
        # get rid of all the secondary elements like SVG, etc
        economy_elements_tree = scraped_page.build_economy_elements_tree()
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
            economy_elements_tree_dumped = scraped_page.build_economy_elements_tree(percent_to_keep=2 / 3)
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
