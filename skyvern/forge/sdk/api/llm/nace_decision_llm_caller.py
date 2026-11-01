"""Nace Drex Max decision caller for Skyvern's bounded-choice prompts.

Drex Max (https://console.nace.ai/docs/drex/guides/drex-max) is a decision model rather than a
chat model: one ``POST /v1/systemone`` request carries a ``state`` text, up to eight images and a
few bounded questions, and the answer is a probability per option instead of generated text.
Several Skyvern prompts are bounded questions already (is the goal achieved? which page state?
which action type? which suggestion?), so this module rewrites each of them into System One
questions and rebuilds the JSON object the prompt's existing parser expects. Nothing here
generates free text: ``thoughts``/``reasoning`` fields carry the decision and its probability.

Opt-in only. ``ENABLE_NACE=true`` registers the ``NACE_DREX_MAX`` LLM key, ``forge_app`` builds
``DECISION_LLM_API_HANDLER`` from it, and only the prompts in ``NACE_DECISION_PROMPT_NAMES`` are
routed through it (``get_decision_llm_api_handler`` in ``api_handler_factory``). Any other prompt
sent to the key is rejected before a request is made. Routing stays inside ``LLMCaller``
(``_call_nace_decision``), so artifact persistence, token accounting and error classification are
the base class's, as for UI-TARS and Yutori Navigator.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import math
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

import httpx
import structlog
from litellm.utils import ModelResponse

from skyvern.forge.sdk.api.llm.exceptions import (
    BaseLLMError,
    InvalidLLMResponseFormat,
    LLMProviderError,
    LLMProviderErrorRetryableTask,
)

LOG = structlog.get_logger()

NACE_DECISION_LLM_KEY = "NACE_DREX_MAX"
NACE_DEFAULT_API_BASE = "https://console.nace.ai"
NACE_DEFAULT_MODEL = "drex-max"
NACE_SYSTEMONE_PATH = "/v1/systemone"
# https://console.nace.ai/docs/drex/guides/drex-max: 8 images per request (Drex resizes each to at
# most 1,638,400 px), 255 options per choice question, 512 questions per request.
NACE_MAX_IMAGES = 8
NACE_MAX_CHOICE_OPTIONS = 255
# Drex rejects request bodies over 4 MiB, and downsizes every image to about 1.6 MP anyway.
NACE_MAX_BODY_BYTES = 4_194_304
NACE_MAX_IMAGE_PIXELS = 1_638_400
# Drex waits up to 55 s server-side, so the client must wait longer than that.
NACE_MIN_TIMEOUT_SECONDS = 60.0
# https://console.nace.ai/docs/drex/reference/models: models bill input tokens only (output is
# free), per million input tokens. drex-latest is an alias of drex-v2.0. Only drex-max and
# drex-max-v1.1 read images; drex-v2.0 and drex-latest are text only and answer 422 to an image.
_NACE_USD_PER_MILLION_INPUT_TOKENS: dict[str, float] = {
    "drex-max": 0.03,
    "drex-max-v1.1": 0.03,
    "drex-v2.0": 0.06,
    "drex-latest": 0.06,
}
_NACE_IMAGE_MODELS = frozenset({"drex-max", "drex-max-v1.1"})
NACE_USD_PER_INPUT_TOKEN = _NACE_USD_PER_MILLION_INPUT_TOKENS[NACE_DEFAULT_MODEL] / 1_000_000

CHECK_USER_GOAL_PROMPT_NAME = "check-user-goal"
CHECK_USER_GOAL_AFTER_CLICK_PROMPT_NAME = "check-user-goal-after-click"
CHECK_USER_GOAL_WITH_TERMINATION_PROMPT_NAME = "check-user-goal-with-termination"
DECISIVE_CRITERION_VALIDATE_PROMPT_NAME = "decisive-criterion-validate"
PAGE_CLASSIFY_PROMPT_NAME = "page-classify"
INFER_ACTION_TYPE_PROMPT_NAME = "infer-action-type"
AUTO_COMPLETION_CHOOSE_OPTION_PROMPT_NAME = "auto-completion-choose-option"

# The only prompts this caller answers. Each one is a bounded decision whose JSON reply the recipe
# below rebuilds.
NACE_DECISION_PROMPT_NAMES: frozenset[str] = frozenset(
    {
        CHECK_USER_GOAL_PROMPT_NAME,
        CHECK_USER_GOAL_AFTER_CLICK_PROMPT_NAME,
        CHECK_USER_GOAL_WITH_TERMINATION_PROMPT_NAME,
        DECISIVE_CRITERION_VALIDATE_PROMPT_NAME,
        PAGE_CLASSIFY_PROMPT_NAME,
        INFER_ACTION_TYPE_PROMPT_NAME,
        AUTO_COMPLETION_CHOOSE_OPTION_PROMPT_NAME,
    }
)

_DECISION_NOTE_PREFIX = "Drex Max decision"


class NaceDecisionPromptError(BaseLLMError):
    """The prompt cannot be turned into a System One request.

    Either the prompt is unsupported or its options were not found.
    """

    def __init__(self, prompt_name: str | None, detail: str) -> None:
        super().__init__(f"Nace Drex Max cannot answer prompt {prompt_name!r}: {detail}")
        self.prompt_name = prompt_name


class NaceDecisionAPIError(Exception):
    """A non-2xx System One reply, wrapped in the LLMProviderError the caller raises."""

    def __init__(self, status_code: int, message: str, request_id: str | None = None) -> None:
        suffix = f" (request_id={request_id})" if request_id else ""
        super().__init__(f"HTTP {status_code}: {message}{suffix}")
        self.status_code = status_code
        self.request_id = request_id


def nace_model_supports_images(model: str | None) -> bool:
    """Whether ``model`` accepts screenshots.

    True for drex-max and its versions; drex-v2.0 and drex-latest are text only.
    """
    model = model or ""
    return model in _NACE_IMAGE_MODELS or model.startswith("drex-max-")


def nace_decision_price_per_input_token(model: str | None) -> float | None:
    """USD per input token for a Nace model id, or None when the model is not in the price list."""
    model = model or ""
    price = _NACE_USD_PER_MILLION_INPUT_TOKENS.get(model)
    if price is None and model.startswith("drex-max-"):
        price = _NACE_USD_PER_MILLION_INPUT_TOKENS[NACE_DEFAULT_MODEL]
    return None if price is None else price / 1_000_000


def nace_decision_cost_usd(input_tokens: int, model: str | None = None) -> float | None:
    """Cost of one call in USD for the model that served it; None when its price is unknown."""
    price = nace_decision_price_per_input_token(model or NACE_DEFAULT_MODEL)
    return None if price is None else input_tokens * price


# ---------------------------------------------------------------------------
# System One questions and answers
# ---------------------------------------------------------------------------

Decoder = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class DecisionRequest:
    """The System One questions for one prompt and the decoder for the prompt's JSON reply."""

    questions: dict[str, dict[str, Any]]
    decode: Decoder


def _noul(instructions: str) -> dict[str, Any]:
    return {"type": "noul", "instructions": instructions}


def _choice(instructions: str, criteria: Mapping[str, str | None]) -> dict[str, Any]:
    # criteria is an object keyed by option; values are plain strings (or null), never nested
    # objects.
    return {"type": "choice", "instructions": instructions, "criteria": dict(criteria)}


def _probability(value: Any, what: str) -> float:
    """A probability in [0, 1]; booleans, non-numbers, NaN/inf and other values are rejected."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidLLMResponseFormat(f"{what} is not a probability: {value!r}")
    probability = float(value)
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        raise InvalidLLMResponseFormat(f"{what} is outside [0, 1]: {value!r}")
    return probability


def _noul_probability(answers: Mapping[str, Any], name: str) -> float:
    answer = answers.get(name)
    if not isinstance(answer, Mapping) or "noul" not in answer:
        raise InvalidLLMResponseFormat(f"missing noul answer {name!r}: {json.dumps(answers)[:500]}")
    return _probability(answer["noul"], f"noul answer {name!r}")


def _choice_answer(answers: Mapping[str, Any], name: str) -> tuple[str, float, dict[str, float]]:
    answer = answers.get(name)
    if not isinstance(answer, Mapping) or not isinstance(answer.get("choice"), str):
        raise InvalidLLMResponseFormat(
            f"missing choice answer {name!r}: {json.dumps(answers)[:500]}",
        )
    raw_probabilities = answer.get("probabilities")
    if not isinstance(raw_probabilities, Mapping) or not raw_probabilities:
        raise InvalidLLMResponseFormat(
            f"choice answer {name!r} has no probabilities: {json.dumps(answer)[:500]}",
        )
    probabilities = {
        str(option): _probability(probability, f"probability of {option!r} in {name!r}")
        for option, probability in raw_probabilities.items()
    }
    choice = answer["choice"]
    if choice not in probabilities:
        raise InvalidLLMResponseFormat(f"choice {choice!r} of {name!r} has no probability")
    confidence = answer.get("confidence")
    if confidence is None:
        confidence = probabilities[choice]
    return choice, _probability(confidence, f"confidence of {name!r}"), probabilities


def _binary_confidence(probability: float) -> float:
    return max(probability, 1.0 - probability)


def _note(what: str, probability: float) -> str:
    return f"{_DECISION_NOTE_PREFIX}: {what} (p={probability:.3f})"


def _fenced_block_after(prompt: str, heading: str) -> str | None:
    """The contents of the fenced block right after the ``heading`` line of a prompt, or None.

    Anchored to a whole line: the templates also mention these headings inside their reply-format
    comments.
    """
    pattern = rf"^{re.escape(heading)}\n```[a-z]*\n(.*?)\n```"
    match = re.search(
        pattern,
        prompt,
        re.MULTILINE | re.DOTALL,
    )
    return match.group(1) if match else None


# -- check-user-goal / check-user-goal-after-click -------------------------------------------------

_CHECK_USER_GOAL_INSTRUCTIONS = (
    "The text is a Skyvern verification prompt: a user goal, user details, the action history and "
    "the elements parsed from the current page; any images are screenshots of that page. Judge it "
    "the way the prompt instructs. Has the user goal been completed?"
)


def _check_user_goal_request(prompt: str) -> DecisionRequest:
    questions = {"user_goal_achieved": _noul(_CHECK_USER_GOAL_INSTRUCTIONS)}

    def decode(answers: dict[str, Any]) -> dict[str, Any]:
        probability = _noul_probability(answers, "user_goal_achieved")
        achieved = probability >= 0.5
        return {
            "thoughts": _note(
                "user goal achieved" if achieved else "user goal not achieved",
                probability,
            ),
            "user_goal_achieved": achieved,
            "confidence_float": _binary_confidence(probability),
        }

    return DecisionRequest(questions=questions, decode=decode)


# -- check-user-goal-with-termination --------------------------------------------------------------

_VERIFICATION_STATUSES: dict[str, str] = {
    "complete": "the user goal has been fully achieved (per the complete criterion, if given)",
    "terminate": (
        "the goal can never be achieved: a permanent error, a missing file, or an explicit "
        "termination condition from the user goal is met"
    ),
    "continue": (
        "the goal is not achieved yet but more steps could achieve it: the page is loading, "
        "something must be clicked, or a different action can be tried"
    ),
}

_FAILURE_CATEGORIES: dict[str, str] = {
    "ANTI_BOT_DETECTION": "CAPTCHA, Cloudflare challenge or bot detection",
    "BROWSER_ERROR": "browser crash or page closed",
    "NAVIGATION_FAILURE": "404 or redirect",
    "PAGE_LOAD_TIMEOUT": "page load timeout",
    "AUTH_FAILURE": "login failed or MFA",
    "LLM_REASONING_ERROR": "wrong element or wrong action",
    "CREDENTIAL_ERROR": "missing credentials",
    "ELEMENT_NOT_FOUND": "target element missing",
    "WRONG_PAGE_STATE": "unexpected page",
    "DATA_EXTRACTION_FAILURE": "extraction failed",
    "WEBSITE_ERROR": "the website's own server failed or is down (HTTP 5xx, CDN error, outage)",
    "INFRASTRUCTURE_ERROR": "Skyvern's own platform failed (workers, browser pool, proxies)",
    "UNKNOWN": "the root cause cannot be determined",
}


def _check_user_goal_with_termination_request(prompt: str) -> DecisionRequest:
    questions = {
        "status": _choice(
            "The text is a Skyvern verification prompt: a user goal, user details, the action "
            "history and the elements parsed from the current page; any images are screenshots of "
            "that page. Judge it the way the prompt instructs. Which status describes the task?",
            _VERIFICATION_STATUSES,
        ),
        "failure_category": _choice(
            "If the goal can never be achieved, which category best explains the root cause?",
            _FAILURE_CATEGORIES,
        ),
    }

    def decode(answers: dict[str, Any]) -> dict[str, Any]:
        status, confidence, _ = _choice_answer(answers, "status")
        if status not in _VERIFICATION_STATUSES:
            raise InvalidLLMResponseFormat(f"unexpected verification status {status!r}")
        failure_categories: list[dict[str, Any]] = []
        if status == "terminate":
            category, category_confidence, _ = _choice_answer(answers, "failure_category")
            failure_categories.append(
                {
                    "category": category,
                    "confidence_float": category_confidence,
                    "reasoning": _note(f"failure category {category}", category_confidence),
                }
            )
        return {
            "thoughts": _note(f"status {status}", confidence),
            "status": status,
            "failure_categories": failure_categories,
            "confidence_float": confidence,
        }

    return DecisionRequest(questions=questions, decode=decode)


# -- decisive-criterion-validate -------------------------------------------------------------------

_DECISIVE_ACTIONS: dict[str, str] = {
    "COMPLETE": "the complete criterion is met",
    "TERMINATE": "the terminate criterion is met, or a given complete criterion is not met",
}


def _decisive_criterion_validate_request(prompt: str) -> DecisionRequest:
    questions = {
        "action": _choice(
            "The text is a Skyvern validation prompt: a complete and/or terminate criterion, user "
            "details and, unless it says the page is not relevant, the current URL and the "
            "elements parsed from the page; any images are screenshots of that page. Judge it the "
            "way the prompt instructs. Which action applies?",
            _DECISIVE_ACTIONS,
        )
    }

    def decode(answers: dict[str, Any]) -> dict[str, Any]:
        action, confidence, _ = _choice_answer(answers, "action")
        if action not in _DECISIVE_ACTIONS:
            raise InvalidLLMResponseFormat(f"unexpected validation action {action!r}")
        note = _note(f"action {action}", confidence)
        return {
            "page_info": note,
            "thoughts": note,
            "actions": [{"reasoning": note, "confidence_float": confidence, "action_type": action}],
        }

    return DecisionRequest(questions=questions, decode=decode)


# -- page-classify ---------------------------------------------------------------------------------

_PAGE_CLASSIFY_OPTIONS_START = "Classify the page into ONE of the following options:"
_PAGE_CLASSIFY_OPTIONS_END = "Reply in JSON format:"
_PAGE_CLASSIFY_OPTION = re.compile(r'^- "(?P<key>[^"]+)": (?P<description>.*)$', re.MULTILINE)


def parse_page_classify_options(prompt: str) -> dict[str, str]:
    """The option keys and descriptions rendered by page-classify.j2 (including UNKNOWN)."""
    start = prompt.find(_PAGE_CLASSIFY_OPTIONS_START)
    if start < 0:
        return {}
    end = prompt.find(_PAGE_CLASSIFY_OPTIONS_END, start)
    section = prompt[start : end if end > start else len(prompt)]
    matches = _PAGE_CLASSIFY_OPTION.finditer(section)
    return {match["key"]: match["description"].strip() for match in matches}


def _page_classify_request(prompt: str) -> DecisionRequest:
    options = parse_page_classify_options(prompt)
    if not options:
        raise NaceDecisionPromptError(
            PAGE_CLASSIFY_PROMPT_NAME,
            "no classification options found in the prompt",
        )
    questions = {
        "classification": _choice(
            "The text gives the current URL and the first characters of the page text. Which "
            "option best describes the current state of the page?",
            options,
        )
    }

    def decode(answers: dict[str, Any]) -> dict[str, Any]:
        classification, confidence, _ = _choice_answer(answers, "classification")
        return {
            "reasoning": _note(f"classification {classification}", confidence),
            "classification": classification,
            "confidence": confidence,
        }

    return DecisionRequest(questions=questions, decode=decode)


# -- infer-action-type -----------------------------------------------------------------------------

_UNKNOWN_ACTION = "UNKNOWN_ACTION"
_ACTION_TYPES: dict[str, str] = {
    "CLICK": "the user wants to click",
    "INPUT_TEXT": "the user wants to input text",
    "UPLOAD_FILE": "the user wants to upload a file",
    "SELECT_OPTION": "the user wants to select an option",
    "HOVER": (
        "the user wants to move the mouse over an element without clicking, such as to reveal a "
        "hover-only menu or tooltip"
    ),
    _UNKNOWN_ACTION: "none of the defined action types matches the instruction",
}


def _infer_action_type_request(prompt: str) -> DecisionRequest:
    questions = {
        "action_type": _choice(
            "The text is a single browser action instruction. Which action type does it ask for?",
            _ACTION_TYPES,
        )
    }

    def decode(answers: dict[str, Any]) -> dict[str, Any]:
        action_type, confidence, probabilities = _choice_answer(answers, "action_type")
        if action_type == _UNKNOWN_ACTION:
            return {
                "thought": _note("no defined action type matches the instruction", confidence),
                "inferred_actions": [],
                "error": _UNKNOWN_ACTION,
            }
        if action_type not in _ACTION_TYPES:
            raise InvalidLLMResponseFormat(f"unexpected action type {action_type!r}")
        known = [(name, p) for name, p in probabilities.items() if name in _ACTION_TYPES]
        ranked = sorted(
            known,
            key=lambda item: item[1],
            reverse=True,
        )
        if not ranked or ranked[0][0] != action_type:
            ranked = [(action_type, confidence)]
        return {
            "thought": _note(f"action type {action_type}", confidence),
            "inferred_actions": [
                {
                    "reasoning": _note(f"action type {name}", probability),
                    "confidence_float": probability,
                    "action_type": name,
                }
                for name, probability in ranked
                if name != _UNKNOWN_ACTION
            ],
            "error": None,
        }

    return DecisionRequest(questions=questions, decode=decode)


# -- auto-completion-choose-option -----------------------------------------------------------------

_AUTO_COMPLETION_SEARCH_MARKER = "or directly search with the input value"
_AUTO_COMPLETION_ELEMENTS_HEADING = "HTML elements:"
_AUTO_COMPLETION_EMERGING_HEADING = "IDs for emerging HTML elements"
_NO_OPTION = "NONE"
_MAX_CANDIDATE_DESCRIPTION_CHARS = 240
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)


@dataclass
class CandidateElement:
    """An element with a Skyvern id rendered in a prompt, as a choice option."""

    element_id: str
    tag: str
    attributes: dict[str, str]
    text_parts: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        """The text of the element's subtree with whitespace collapsed."""
        return " ".join(" ".join(self.text_parts).split())

    def describe(self) -> str:
        """A one-line tag, attributes and text description used as the choice option's criterion."""
        attributes = " ".join(f'{key}="{value}"' for key, value in self.attributes.items())
        description = f"<{self.tag}{' ' + attributes if attributes else ''}> {self.text}".strip()
        return description[:_MAX_CANDIDATE_DESCRIPTION_CHARS]


class _CandidateElementParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.candidates: list[CandidateElement] = []
        self._open: list[CandidateElement | None] = []

    def _start(self, tag: str, attrs: list[tuple[str, str | None]]) -> CandidateElement | None:
        attributes = {key: value for key, value in attrs if value}
        element_id = attributes.pop("id", None)
        if not element_id:
            return None
        candidate = CandidateElement(element_id=element_id, tag=tag, attributes=attributes)
        self.candidates.append(candidate)
        return candidate

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        candidate = self._start(tag, attrs)
        if tag not in _VOID_TAGS:
            self._open.append(candidate)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if self._open:
            self._open.pop()

    def handle_data(self, data: str) -> None:
        for candidate in self._open:
            if candidate is not None:
                candidate.text_parts.append(data)


def parse_candidate_elements(html: str) -> list[CandidateElement]:
    """Every element with an ``id`` attribute in a rendered element tree, with its subtree text."""
    parser = _CandidateElementParser()
    parser.feed(html)
    parser.close()
    return parser.candidates


def _quoted_in(element_id: str, rendered_ids: str) -> bool:
    """Whether the id appears quoted in the list/set literal the template renders."""
    return f"'{element_id}'" in rendered_ids or f'"{element_id}"' in rendered_ids


def _auto_completion_request(prompt: str) -> DecisionRequest:
    is_search = _AUTO_COMPLETION_SEARCH_MARKER in prompt
    html = _fenced_block_after(prompt, _AUTO_COMPLETION_ELEMENTS_HEADING) or ""
    candidates = parse_candidate_elements(html)
    emerging = _fenced_block_after(prompt, _AUTO_COMPLETION_EMERGING_HEADING)
    if emerging:
        # The template renders the ids as a Python list/set literal, so each id appears quoted.
        emerging_candidates = [c for c in candidates if _quoted_in(c.element_id, emerging)]
        if emerging_candidates:
            candidates = emerging_candidates
    if len(candidates) > NACE_MAX_CHOICE_OPTIONS - 1:
        LOG.info(
            "Drex Max choice question capped; dropping trailing auto-completion candidates",
            candidates=len(candidates),
            kept=NACE_MAX_CHOICE_OPTIONS - 1,
        )
        candidates = candidates[: NACE_MAX_CHOICE_OPTIONS - 1]
    values = {candidate.element_id: candidate.text for candidate in candidates}

    questions: dict[str, dict[str, Any]] = {
        "auto_completion_attempt": _noul(
            "The text shows an input field, the value typed into it, the user goal and details, "
            "and the elements that are on the page now. Did the page attempt auto-completion for "
            "the typed value? Several suggestions count, and so does a message like 'No results' "
            "or 'No match'."
        )
    }
    if candidates:
        criteria: dict[str, str | None] = {c.element_id: c.describe() for c in candidates}
        criteria[_NO_OPTION] = "no element is a meaningful suggestion for the typed value"
        questions["option"] = _choice(
            "Each option is an element on the page, keyed by its id. Which element is the "
            "auto-completion "
            f"suggestion that best matches the user goal, the user details and the field context? "
            f"Choose {_NO_OPTION} "
            "when no element is a meaningful suggestion (a 'No results' message is not one).",
            criteria,
        )
        # Relevance is its own question: with several equally good suggestions the choice
        # probability is split between them, which says nothing about whether the selected one
        # satisfies the goal.
        questions["relevance"] = _noul(
            "Does the auto-completion suggestion that best matches the typed value satisfy the "
            "user goal, the user details and the field context? Answer yes when at least one "
            "element is a fitting suggestion, even if several elements are equally good."
        )
    if is_search:
        questions["direct_searching"] = _noul(
            "Is searching directly with the typed input value a better way to reach the user goal "
            "than selecting any of the suggestions?"
        )

    def decode(answers: dict[str, Any]) -> dict[str, Any]:
        attempt_probability = _noul_probability(answers, "auto_completion_attempt")
        element_id: str | None = None
        confidence = _binary_confidence(attempt_probability)
        relevance = 0.0
        if "option" in questions:
            option, confidence, _ = _choice_answer(answers, "option")
            if option != _NO_OPTION:
                if option not in values:
                    raise InvalidLLMResponseFormat(f"unexpected auto-completion option {option!r}")
                element_id = option
                relevance = round(_noul_probability(answers, "relevance"), 2)
        result: dict[str, Any] = {
            "thought": _note(
                "auto-completion attempt" if attempt_probability >= 0.5 else "no attempt",
                attempt_probability,
            ),
            "auto_completion_attempt": attempt_probability >= 0.5,
            "reasoning": _note(
                f"option {element_id}" if element_id else "no suitable option",
                confidence,
            ),
            "confidence_float": confidence,
            "relevance_float": relevance,
            "value": values.get(element_id, "") if element_id else "",
            "id": element_id,
        }
        if is_search:
            direct_probability = _noul_probability(answers, "direct_searching")
            result["direct_searching"] = direct_probability >= 0.5
        return result

    return DecisionRequest(questions=questions, decode=decode)


_RECIPES: dict[str, Callable[[str], DecisionRequest]] = {
    CHECK_USER_GOAL_PROMPT_NAME: _check_user_goal_request,
    CHECK_USER_GOAL_AFTER_CLICK_PROMPT_NAME: _check_user_goal_request,
    CHECK_USER_GOAL_WITH_TERMINATION_PROMPT_NAME: _check_user_goal_with_termination_request,
    DECISIVE_CRITERION_VALIDATE_PROMPT_NAME: _decisive_criterion_validate_request,
    PAGE_CLASSIFY_PROMPT_NAME: _page_classify_request,
    INFER_ACTION_TYPE_PROMPT_NAME: _infer_action_type_request,
    AUTO_COMPLETION_CHOOSE_OPTION_PROMPT_NAME: _auto_completion_request,
}


def build_decision_request(prompt_name: str | None, prompt: str) -> DecisionRequest:
    """The System One questions for ``prompt_name`` rendered as ``prompt``.

    Rejects prompts this caller cannot answer.
    """
    recipe = _RECIPES.get(prompt_name or "")
    if recipe is None:
        supported = ", ".join(sorted(NACE_DECISION_PROMPT_NAMES))
        raise NaceDecisionPromptError(
            prompt_name,
            f"only bounded-choice prompts are supported: {supported}",
        )
    return recipe(prompt)


# ---------------------------------------------------------------------------
# Messages -> request, API call, answers -> ModelResponse
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT_HEADING = "Instructions from the workflow (follow them when judging the text below):"


def _split_system_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """The text of the system messages and the remaining messages."""
    system_messages = [message for message in messages if message.get("role") == "system"]
    system_text, _ = extract_state_and_images(system_messages)
    return system_text, [message for message in messages if message.get("role") != "system"]


def extract_state_and_images(messages: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """Flatten chat messages into the System One state text and image data URLs (first 8 kept)."""
    texts: list[str] = []
    images: list[str] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
            continue
        for block in content or []:
            if not isinstance(block, Mapping):
                continue
            block_type = block.get("type")
            if block_type == "text" and block.get("text"):
                texts.append(str(block["text"]))
            elif block_type == "image_url":
                url = block.get("image_url", {}).get("url")
                if url:
                    images.append(str(url))
            elif block_type == "image":
                source = block.get("source") or {}
                if source.get("type") == "base64" and source.get("data"):
                    media_type = source.get("media_type", "image/png")
                    images.append(f"data:{media_type};base64,{source['data']}")
    if len(images) > NACE_MAX_IMAGES:
        LOG.info(
            "Drex Max accepts 8 images per request; dropping the rest",
            image_count=len(images),
        )
        images = images[:NACE_MAX_IMAGES]
    return "\n\n".join(texts), images


_DATA_URL = re.compile(r"^data:(?P<media>[^;,]+);base64,(?P<data>.*)$", re.DOTALL)


def _body_bytes(state: str, questions: dict[str, dict[str, Any]], images: list[str]) -> int:
    body = {"model": NACE_DEFAULT_MODEL, "state": state, "questions": questions, "images": images}
    return len(json.dumps(body).encode("utf-8"))


def _reencode(data_url: str, *, jpeg: bool, max_pixels: int = NACE_MAX_IMAGE_PIXELS) -> str:
    """Downscale a base64 image data URL to ``max_pixels`` (PNG, or JPEG when ``jpeg``)."""
    from PIL import Image

    match = _DATA_URL.match(data_url)
    if match is None:
        return data_url
    try:
        image = Image.open(io.BytesIO(base64.b64decode(match["data"])))
        image.load()
    except (binascii.Error, OSError, ValueError):
        return data_url
    pixels = image.width * image.height
    if pixels > max_pixels:
        scale = math.sqrt(max_pixels / pixels)
        size = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
        image = image.resize(size, Image.Resampling.LANCZOS)
    elif not jpeg:
        return data_url
    out = io.BytesIO()
    if jpeg:
        image.convert("RGB").save(out, format="JPEG", quality=80)
        media = "image/jpeg"
    else:
        image.save(out, format="PNG")
        media = "image/png"
    return f"data:{media};base64,{base64.b64encode(out.getvalue()).decode('ascii')}"


def fit_images_to_body_limit(
    state: str,
    questions: dict[str, dict[str, Any]],
    images: list[str],
    limit: int = NACE_MAX_BODY_BYTES,
) -> list[str]:
    """Keep the request body under ``limit`` bytes by shrinking images, then dropping the oldest.

    Images are downscaled to Drex's own pixel budget first (nothing Drex would use is lost). If
    the body is still too large the oldest images are dropped, the newest is always kept, and as
    a last resort the newest is re-encoded as JPEG.
    """
    images = [_reencode(image, jpeg=False) for image in images]
    while len(images) > 1 and _body_bytes(state, questions, images) > limit:
        images = images[1:]
    if images and _body_bytes(state, questions, images) > limit:
        images = [_reencode(images[-1], jpeg=True)]
    if images and _body_bytes(state, questions, images) > limit:
        LOG.warning("Drex Max request body is over the size limit even with one image", limit=limit)
    return images


class NaceDecisionClient:
    """Minimal async client for ``POST {base_url}/v1/systemone``.

    4xx replies are deterministic (bad key, invalid request) and surface as ``LLMProviderError``;
    408, 429, 529 and 5xx are transient and surface as ``LLMProviderErrorRetryableTask``, like the
    litellm paths do.
    """

    def __init__(
        self,
        api_key: str | None,
        base_url: str = NACE_DEFAULT_API_BASE,
        model: str = NACE_DEFAULT_MODEL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "User-Agent": "skyvern",
                    "X-Nace-Client": _client_header(),
                },
                transport=self._transport,
                timeout=NACE_MIN_TIMEOUT_SECONDS,
            )
        return self._client

    async def aclose(self) -> None:
        """Close the underlying HTTP client, if one was opened."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def decide(
        self,
        *,
        state: str,
        questions: dict[str, dict[str, Any]],
        images: list[str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """POST one System One request and return the JSON reply.

        Errors are classified as retryable or not by status code.
        """
        body: dict[str, Any] = {"model": self.model, "state": state, "questions": questions}
        if images:
            body["images"] = images
        request_timeout = max(timeout or 0.0, NACE_MIN_TIMEOUT_SECONDS)
        try:
            response = await self._http().post(
                NACE_SYSTEMONE_PATH,
                json=body,
                timeout=request_timeout,
            )
        except httpx.HTTPError as e:
            raise LLMProviderErrorRetryableTask(NACE_DECISION_LLM_KEY, cause=e) from e
        if response.status_code >= 400:
            error = NaceDecisionAPIError(
                response.status_code, _error_message(response), response.headers.get("x-request-id")
            )
            if response.status_code in (408, 429, 529) or response.status_code >= 500:
                raise LLMProviderErrorRetryableTask(NACE_DECISION_LLM_KEY, cause=error) from error
            raise LLMProviderError(NACE_DECISION_LLM_KEY, cause=error) from error
        try:
            payload = response.json()
        except ValueError as e:
            raise LLMProviderErrorRetryableTask(NACE_DECISION_LLM_KEY, cause=e) from e
        if not isinstance(payload, dict):
            raise InvalidLLMResponseFormat(
                f"System One reply is not an object: {str(payload)[:200]}",
            )
        return payload


def _client_header() -> str:
    """``skyvern/<version>``, naming this integration for Nace's usage attribution."""
    # imported here: analytics pulls in the CLI and posthog, which the caller doesn't need to load
    from skyvern.analytics import get_oss_version

    version = re.sub(r"[^A-Za-z0-9 ._@+-]", "-", get_oss_version())
    return f"skyvern/{version}"[:64]


def _error_message(response: httpx.Response) -> str:
    """The API's error message, tolerating any JSON shape.

    The status code decides retries, not this message.
    """
    fallback = response.text[:300]
    try:
        body = response.json()
    except ValueError:
        return fallback
    error = body.get("error") if isinstance(body, Mapping) else None
    if isinstance(error, str):
        return error[:300] or fallback
    if not isinstance(error, Mapping):
        return fallback
    message = str(error.get("message") or fallback)
    issues = error.get("issues")
    if isinstance(issues, list):
        listed = [issue for issue in issues[:5] if isinstance(issue, Mapping)]
        details = [f"{issue.get('path')}: {issue.get('message')}" for issue in listed]
        if details:
            message += " " + "; ".join(details)
    return message


def _to_model_response(
    parsed: dict[str, Any], usage: Mapping[str, Any], model: str, request_id: str | None
) -> ModelResponse:
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    return ModelResponse(
        id=request_id or f"nace-{uuid.uuid4().hex}",
        model=model,
        object="chat.completion",
        choices=[
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(parsed)},
            }
        ],
        usage={
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    )


async def call_nace_decision(
    client: NaceDecisionClient,
    messages: list[dict[str, Any]],
    prompt_name: str | None,
    timeout: float | None = None,
) -> ModelResponse:
    """Answer one bounded-choice prompt with Drex Max and return it as a chat-shaped ModelResponse.

    The content is the JSON object the prompt's parser expects, so ``parse_api_response`` and the
    call sites stay unchanged; ``usage`` carries the System One token counts for cost tracking.
    """
    system_text, user_messages = _split_system_messages(messages)
    user_text, images = extract_state_and_images(user_messages)
    request = build_decision_request(prompt_name, user_text)
    # Constraints from task.workflow_system_prompt ride in the state ahead of the prompt, so the
    # judgment follows them; the recipes only ever read the prompt text.
    state = f"{_SYSTEM_PROMPT_HEADING}\n{system_text}\n\n{user_text}" if system_text else user_text
    if images and not nace_model_supports_images(client.model):
        LOG.info(
            "Model does not read images; sending the text only",
            model=client.model,
            image_count=len(images),
        )
        images = []
    LOG.info(
        "Drex Max decision request",
        prompt_name=prompt_name,
        model=client.model,
        questions=list(request.questions),
        image_count=len(images),
    )
    if images:
        images = await asyncio.to_thread(fit_images_to_body_limit, state, request.questions, images)
    payload = await client.decide(
        state=state,
        questions=request.questions,
        images=images,
        timeout=timeout,
    )
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        raise InvalidLLMResponseFormat(
            f"System One reply has no answers: {json.dumps(payload)[:300]}",
        )
    parsed = request.decode(answers)
    raw_usage = payload.get("usage")
    usage: Mapping[str, Any] = raw_usage if isinstance(raw_usage, Mapping) else {}
    LOG.info(
        "Drex Max decision response",
        prompt_name=prompt_name,
        request_id=payload.get("request_id"),
        evaluation_time_ms=payload.get("evaluation_time_ms"),
        input_tokens=usage.get("input_tokens"),
    )
    served_model = payload.get("model")
    return _to_model_response(
        parsed,
        usage,
        model=served_model if isinstance(served_model, str) and served_model else client.model,
        request_id=payload.get("request_id"),
    )
