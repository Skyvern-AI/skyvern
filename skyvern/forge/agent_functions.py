import asyncio
import copy
import hashlib
from typing import Dict, List

import structlog
from playwright.async_api import Page

from skyvern.constants import SKYVERN_ID_ATTR
from skyvern.exceptions import StepUnableToExecuteError, SVGConversionFailed
from skyvern.forge import app
from skyvern.forge.async_operations import AsyncOperation
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.models import Organization, Step, StepStatus
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.webeye.browser_factory import BrowserState
from skyvern.webeye.scraper.scraper import ELEMENT_NODE_ATTRIBUTES, CleanupElementTreeFunc, json_to_html

LOG = structlog.get_logger()

USELESS_SVG_ATTRIBUTE = [SKYVERN_ID_ATTR, "id", "aria-describedby"]
SVG_RETRY_ATTEMPT = 3


def _remove_rect(element: dict) -> None:
    if "rect" in element:
        del element["rect"]


def _get_svg_cache_key(hash: str) -> str:
    return f"skyvern:svg:{hash}"


def _remove_skyvern_attributes(element: Dict) -> Dict:
    """
    To get the original HTML element without skyvern attributes
    """
    element_copied = copy.deepcopy(element)
    for attr in ELEMENT_NODE_ATTRIBUTES:
        if element_copied.get(attr):
            del element_copied[attr]

    if "attributes" in element_copied:
        attributes: dict = copy.deepcopy(element_copied.get("attributes", {}))
        for key in attributes.keys():
            if key in USELESS_SVG_ATTRIBUTE:
                del element_copied["attributes"][key]

    children: List[Dict] | None = element_copied.get("children", None)
    if children is None:
        return element_copied

    trimmed_children = []
    for child in children:
        trimmed_children.append(_remove_skyvern_attributes(child))

    element_copied["children"] = trimmed_children
    return element_copied


async def _convert_svg_to_string(task: Task, step: Step, organization: Organization | None, element: Dict) -> None:
    if element.get("tagName") != "svg":
        return

    element_id = element.get("id", "")
    svg_element = _remove_skyvern_attributes(element)
    svg_html = json_to_html(svg_element)
    hash_object = hashlib.sha256()
    hash_object.update(svg_html.encode("utf-8"))
    svg_hash = hash_object.hexdigest()
    svg_key = _get_svg_cache_key(svg_hash)

    svg_shape: str | None = None
    try:
        svg_shape = await app.CACHE.get(svg_key)
    except Exception:
        LOG.warning(
            "Failed to loaded SVG cache",
            exc_info=True,
            key=svg_key,
        )

    if svg_shape:
        LOG.debug("SVG loaded from cache", element_id=element_id, shape=svg_shape)
    else:
        LOG.debug("call LLM to convert SVG to string shape", element_id=element_id)
        svg_convert_prompt = prompt_engine.load_prompt("svg-convert", svg_element=svg_html)

        for retry in range(SVG_RETRY_ATTEMPT):
            try:
                json_response = await app.SECONDARY_LLM_API_HANDLER(prompt=svg_convert_prompt, step=step)
                svg_shape = json_response.get("shape", "")
                if not svg_shape:
                    raise Exception("Empty SVG shape replied by secondary llm")
                LOG.info("SVG converted by LLM", element_id=element_id, shape=svg_shape)
                await app.CACHE.set(svg_key, svg_shape)
                break
            except Exception:
                LOG.exception(
                    "Failed to convert SVG to string shape by secondary llm. Will retry if haven't met the max try attempt after 3s.",
                    element_id=element_id,
                    retry=retry,
                )
                await asyncio.sleep(3)
        else:
            raise SVGConversionFailed(svg_html=svg_html)

    element["attributes"] = dict()
    element["attributes"]["alt"] = svg_shape
    del element["children"]
    return


class AgentFunction:
    async def validate_step_execution(
        self,
        task: Task,
        step: Step,
    ) -> None:
        """
        Checks if the step can be executed. It is called before the step is executed.
        :return: A tuple of whether the step can be executed and a list of reasons why it can't be executed.
        """
        reasons = []
        # can't execute if task status is not running
        has_valid_task_status = task.status == TaskStatus.running
        if not has_valid_task_status:
            reasons.append(f"invalid_task_status:{task.status}")
        # can't execute if the step is already running or completed
        has_valid_step_status = step.status in [StepStatus.created, StepStatus.failed]
        if not has_valid_step_status:
            reasons.append(f"invalid_step_status:{step.status}")
        # can't execute if the task has another step that is running
        steps = await app.DATABASE.get_task_steps(task_id=task.task_id, organization_id=task.organization_id)
        has_no_running_steps = not any(step.status == StepStatus.running for step in steps)
        if not has_no_running_steps:
            reasons.append(f"another_step_is_running_for_task:{task.task_id}")

        can_execute = has_valid_task_status and has_valid_step_status and has_no_running_steps
        if not can_execute:
            raise StepUnableToExecuteError(step_id=step.step_id, reason=f"Cannot execute step. Reasons: {reasons}")

    async def prepare_step_execution(
        self,
        organization: Organization | None,
        task: Task,
        step: Step,
        browser_state: BrowserState,
    ) -> None:
        """
        Get prepared for the step execution. It's called at the first beginning when step running.
        """
        return

    def generate_async_operations(
        self,
        organization: Organization,
        task: Task,
        page: Page,
    ) -> list[AsyncOperation]:
        return []

    def cleanup_element_tree_factory(
        self,
        task: Task,
        step: Step,
        organization: Organization | None = None,
    ) -> CleanupElementTreeFunc:
        async def cleanup_element_tree_func(url: str, element_tree: list[dict]) -> list[dict]:
            """
            Remove rect and attribute.unique_id from the elements.
            The reason we're doing it is to
            1. reduce unnecessary data so that llm get less distrction
            TODO later: 2. reduce tokens sent to llm to save money
            :param elements: List of elements to remove xpaths from.
            :return: List of elements without xpaths.
            """
            queue = []
            for element in element_tree:
                queue.append(element)
            while queue:
                queue_ele = queue.pop(0)
                _remove_rect(queue_ele)
                await _convert_svg_to_string(task, step, organization, queue_ele)
                # TODO: we can come back to test removing the unique_id
                # from element attributes to make sure this won't increase hallucination
                # _remove_unique_id(queue_ele)
                if "children" in queue_ele:
                    queue.extend(queue_ele["children"])
            return element_tree

        return cleanup_element_tree_func
