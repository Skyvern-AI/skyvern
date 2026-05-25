"""
Relative to this file I will have a prompt directory its located ../prompts
In this directory there will be a techniques directory and a directory for each model - gpt-3.5-turbo gpt-4, llama-2-70B, code-llama-7B etc

Each directory will have jinga2 templates for the prompts.
prompts in the model directories can use the techniques in the techniques directory.

Write the code I'd need to load and populate the templates.

I want the following functions:

class PromptEngine:

    def __init__(self, model):
        pass

    def load_prompt(model, prompt_name, prompt_ags) -> str:
        pass
"""

import glob
import os
from difflib import get_close_matches
from pathlib import Path
from typing import Any, List

import structlog
from jinja2 import Environment, FileSystemLoader

from skyvern.constants import SKYVERN_DIR

LOG = structlog.get_logger()

NON_VISION_CONTEXT_HEADER = "Non-vision page context and accessibility tree:"


def _with_non_vision_context(prompt: str, template: str, kwargs: dict[str, Any]) -> str:
    non_vision_page_context = kwargs.get("non_vision_page_context")
    if not non_vision_page_context or template.endswith("-static") or NON_VISION_CONTEXT_HEADER in prompt:
        return prompt
    return f"{prompt.rstrip()}\n\n{NON_VISION_CONTEXT_HEADER}\n```json\n{non_vision_page_context}\n```"


class PromptEngine:
    """
    Class to handle loading and populating Jinja2 templates for prompts.
    """

    def __init__(self, model: str, prompts_dir: Path = SKYVERN_DIR / "forge" / "prompts") -> None:
        """
        Initialize the PromptEngine with the specified model.

        Args:
            model (str): The model to use for loading prompts.
        """
        self.model = model

        try:
            # Get the list of all model directories
            models_dir = os.path.abspath(prompts_dir)
            model_names = [
                os.path.basename(os.path.normpath(d))
                for d in glob.glob(os.path.join(models_dir, "*/"))
                if os.path.isdir(d) and "techniques" not in d
            ]

            self.model = self.get_closest_match(self.model, model_names)

            self.env = Environment(loader=FileSystemLoader(models_dir))
        except Exception:
            LOG.error("Error initializing PromptEngine.", model=model, exc_info=True)
            raise

    @staticmethod
    def get_closest_match(target: str, model_dirs: List[str]) -> str:
        """
        Find the closest match to the target in the list of model directories.

        Args:
            target (str): The target model.
            model_dirs (list): The list of available model directories.

        Returns:
            str: The closest match to the target.
        """
        try:
            matches = get_close_matches(target, model_dirs, n=1, cutoff=0.1)
            return matches[0]
        except Exception:
            LOG.error(
                "Failed to get closest match.",
                target=target,
                model_dirs=model_dirs,
                exc_info=True,
            )
            raise

    def load_prompt(self, template: str, **kwargs: Any) -> str:
        """
        Load and populate the specified template.

        Args:
            template (str): The name of the template to load.
            **kwargs: The arguments to populate the template with.

        Returns:
            str: The populated template.
        """
        try:
            template = "/".join([self.model, template])
            jinja_template = self.env.get_template(f"{template}.j2")
            return _with_non_vision_context(jinja_template.render(**kwargs), template, kwargs)
        except Exception:
            LOG.error(
                "Failed to load prompt.",
                template=template,
                kwargs_keys=kwargs.keys(),
                exc_info=True,
            )
            raise

    def load_prompt_from_string(self, template: str, **kwargs: Any) -> str:
        """
        Load and populate the specified template from a string.

        Args:
            template (str): The template string to load.
            **kwargs: The arguments to populate the template with.

        Returns:
            str: The populated template.
        """
        try:
            jinja_template = self.env.from_string(template)
            return _with_non_vision_context(jinja_template.render(**kwargs), template, kwargs)
        except Exception:
            LOG.error(
                "Failed to load prompt from string.",
                template=template,
                kwargs_keys=kwargs.keys(),
                exc_info=True,
            )
            raise
