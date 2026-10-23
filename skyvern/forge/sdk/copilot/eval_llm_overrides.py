from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType

from fastapi import HTTPException

from skyvern.config import settings
from skyvern.forge.sdk.api.llm.api_handler import LLMAPIHandler
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.forge.sdk.api.llm.custom_llm_registry import is_custom_llm_key

EVAL_LLM_OVERRIDES_HEADER = "x-copilot-eval-llm-overrides"
EVAL_LLM_PROMPT_TYPES = frozenset({"workflow-copilot", "workflow-copilot-fast", "workflow-copilot-raw-secret-safety"})
MAX_EVAL_LLM_OVERRIDES_BYTES = 2048
_CONFIG_KEY = re.compile(r"[A-Za-z0-9_.:/-]{1,160}")


@dataclass(frozen=True)
class EvalLLMOverrides:
    organization_id: str
    config_keys: Mapping[str, str]
    handlers: Mapping[str, LLMAPIHandler]
    sha256: str


@dataclass(frozen=True)
class _BoundOverrides:
    overrides: EvalLLMOverrides
    workflow_permanent_id: str


_overrides: ContextVar[_BoundOverrides | None] = ContextVar("copilot_eval_llm_overrides", default=None)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate prompt type")
        result[key] = value
    return result


def admit_eval_llm_overrides(raw: str | None, organization_id: str) -> EvalLLMOverrides | None:
    if raw is None:
        return None
    try:
        if len(raw.encode("utf-8")) > MAX_EVAL_LLM_OVERRIDES_BYTES:
            raise ValueError("Oversized overrides")
        parsed = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(parsed, dict) or any(
            key not in EVAL_LLM_PROMPT_TYPES or not isinstance(value, str) or _CONFIG_KEY.fullmatch(value) is None
            for key, value in parsed.items()
        ):
            raise ValueError("Invalid prompt mapping")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise HTTPException(status_code=400, detail="Invalid Copilot eval model overrides") from exc
    if not parsed:
        return None
    if organization_id not in settings.WORKFLOW_COPILOT_EVAL_LLM_OVERRIDES_ORGANIZATION_IDS:
        raise HTTPException(
            status_code=403,
            detail="Copilot eval model overrides require an allowlisted Evaluation organization",
        )
    keys: dict[str, str] = dict(sorted(parsed.items()))
    handlers: dict[str, LLMAPIHandler] = {}
    try:
        for prompt_type, key in keys.items():
            if is_custom_llm_key(key) or not LLMConfigRegistry.is_registered(key):
                raise ValueError("Unsupported configuration")
            LLMConfigRegistry.validate_config(key, LLMConfigRegistry.get_config(key))
            handlers[prompt_type] = LLMAPIHandlerFactory.get_llm_api_handler(key)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Unsupported Copilot eval model configuration") from exc
    encoded = json.dumps(keys, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return EvalLLMOverrides(
        organization_id, MappingProxyType(keys), MappingProxyType(handlers), hashlib.sha256(encoded).hexdigest()
    )


@contextmanager
def eval_llm_override_scope(overrides: EvalLLMOverrides | None, workflow_permanent_id: str) -> Iterator[None]:
    token = _overrides.set(_BoundOverrides(overrides, workflow_permanent_id) if overrides else None)
    try:
        yield
    finally:
        _overrides.reset(token)


def eval_llm_override_handler(
    prompt_type: str, workflow_permanent_id: str | None, organization_id: str | None
) -> LLMAPIHandler | None:
    bound = _overrides.get()
    if (
        bound is None
        or bound.workflow_permanent_id != workflow_permanent_id
        or bound.overrides.organization_id != organization_id
    ):
        return None
    return bound.overrides.handlers.get(prompt_type)


def current_eval_llm_overrides() -> EvalLLMOverrides | None:
    bound = _overrides.get()
    return bound.overrides if bound else None
