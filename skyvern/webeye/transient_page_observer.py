"""Capture short-lived visible page text during explicitly scoped browser waits."""

import re
import time
import weakref
from dataclasses import dataclass
from typing import Any

import structlog
from playwright.async_api import Page

from skyvern.errors.errors import UserDefinedError
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.webeye.utils.page import SkyvernFrame

LOG = structlog.get_logger()

TRANSIENT_TEXT_EVENT_LIMIT = 100
TRANSIENT_TEXT_MIN_LENGTH = 8
TRANSIENT_TEXT_MATCH_MIN_LENGTH = 12
TRANSIENT_TEXT_MATCH_WORD_WINDOW = 6
TRANSIENT_TEXT_MAX_LENGTH = 500
TRANSIENT_TEXT_REASONING_SNIPPET_LIMIT = 160
# Heuristic DOM text matches are strong signals, but not equivalent to explicit LLM classification.
TRANSIENT_TEXT_MATCH_CONFIDENCE = 0.9
TRANSIENT_TEXT_BINDING_NAME = "__skyvernRecordTransientText"
TRANSIENT_TEXT_OBSERVER_STATE_KEY = "__skyvernTransientTextObserver"


@dataclass
class _TransientPageTextBinding:
    active_observer: "TransientPageTextObserver | None" = None
    registered: bool = False


_PAGE_TRANSIENT_TEXT_BINDINGS: weakref.WeakKeyDictionary[Any, _TransientPageTextBinding] = weakref.WeakKeyDictionary()


def _get_transient_text_binding(page: Page) -> _TransientPageTextBinding:
    binding = _PAGE_TRANSIENT_TEXT_BINDINGS.get(page)
    if binding is None:
        binding = _TransientPageTextBinding()
        _PAGE_TRANSIENT_TEXT_BINDINGS[page] = binding
    return binding


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _normalize_for_match(text: str) -> str:
    return _normalize_text(text).casefold()


def _has_meaningful_text_overlap(observed_text: str, mapped_description: str) -> bool:
    observed_text = _normalize_for_match(observed_text)
    mapped_description = _normalize_for_match(mapped_description)
    if observed_text in mapped_description or mapped_description in observed_text:
        return True

    observed_words = re.findall(r"\w+", observed_text)
    mapped_words = re.findall(r"\w+", mapped_description)
    mapped_word_text = f" {' '.join(mapped_words)} "
    return len(observed_words) >= TRANSIENT_TEXT_MATCH_WORD_WINDOW and any(
        f" {' '.join(observed_words[idx : idx + TRANSIENT_TEXT_MATCH_WORD_WINDOW])} " in mapped_word_text
        for idx in range(len(observed_words) - TRANSIENT_TEXT_MATCH_WORD_WINDOW + 1)
    )


def _append_text_event(events: list[dict[str, Any]], payload: Any) -> None:
    if not isinstance(payload, dict) or not isinstance(raw_text := payload.get("text"), str):
        return

    text = _normalize_text(raw_text)
    if len(text) < TRANSIENT_TEXT_MIN_LENGTH:
        return

    text = text[:TRANSIENT_TEXT_MAX_LENGTH]
    if any(event.get("text") == text for event in events):
        return

    event: dict[str, Any] = {}
    for key in ("timestamp_ms", "tag", "role", "aria_live"):
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, str | int | float) or value is None:
            event[key] = value
    event["text"] = text
    events.append(event)
    if len(events) > TRANSIENT_TEXT_EVENT_LIMIT:
        del events[: len(events) - TRANSIENT_TEXT_EVENT_LIMIT]


def _format_observed_text_reasoning(observed_texts: list[str]) -> str:
    snippets = []
    for text in observed_texts[:3]:
        if len(text) > TRANSIENT_TEXT_REASONING_SNIPPET_LIMIT:
            text = f"{text[:TRANSIENT_TEXT_REASONING_SNIPPET_LIMIT]}..."
        snippets.append(text)
    return " | ".join(snippets)


class TransientPageTextObserver:
    def __init__(
        self,
        page: Page,
        *,
        task_id: str | None = None,
        step_id: str | None = None,
        workflow_run_id: str | None = None,
    ) -> None:
        self.page = page
        self.events: list[dict[str, Any]] = []
        self._binding_state: _TransientPageTextBinding | None = None
        self._started_at = time.monotonic()
        self._document_install_attempt_count = 0
        self._document_install_success_count = 0
        self._log_context = {
            "task_id": task_id,
            "step_id": step_id,
            "workflow_run_id": workflow_run_id,
        }

    async def start(self, *, scan_initial_visible_state: bool = True) -> None:
        binding_state = _get_transient_text_binding(self.page)
        previous_active_observer = binding_state.active_observer

        def record_text_event(_source: dict[str, Any], payload: Any) -> None:
            if binding_state.active_observer is not None:
                _append_text_event(binding_state.active_observer.events, payload)

        try:
            self._document_install_attempt_count += 1
            if not binding_state.registered:
                await self.page.expose_binding(TRANSIENT_TEXT_BINDING_NAME, record_text_event)
                binding_state.registered = True
            binding_state.active_observer = self
            await SkyvernFrame.evaluate(
                frame=self.page,
                expression="""
                ({ bindingName, stateKey, minLength, maxLength, scanInitialVisibleState }) => {
                  const key = stateKey;
                  const normalize = (value) => String(value || "").replace(/\\s+/g, " ").trim();
                  const previousState = window[key];

                  const isVisible = (element) => {
                    if (!element || !(element instanceof Element)) return false;
                    const style = window.getComputedStyle(element);
                    if (
                      !style ||
                      style.display === "none" ||
                      style.visibility === "hidden" ||
                      style.opacity === "0"
                    ) {
                      return false;
                    }
                    const rect = element.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                  };

                  const emit = (node) => {
                    const element = node?.nodeType === Node.ELEMENT_NODE ? node : node?.parentElement;
                    if (!element || !isVisible(element)) return;
                    let text = element instanceof HTMLElement && typeof element.innerText === "string"
                      ? element.innerText
                      : element.textContent || "";
                    text = normalize(text);
                    if (text.length < minLength) return;
                    if (text.length > maxLength) text = text.slice(0, maxLength);
                    Promise.resolve(window[bindingName]({
                      text,
                      timestamp_ms: Date.now(),
                      tag: element.tagName || null,
                      role: element.getAttribute("role"),
                      aria_live: element.getAttribute("aria-live"),
                    })).catch(() => {});
                  };

                  let observer;
                  try {
                    observer = new MutationObserver((mutations) => {
                      for (const mutation of mutations) {
                        for (const node of mutation.addedNodes) emit(node);
                        if (mutation.type === "characterData" || mutation.type === "attributes") {
                          emit(mutation.target);
                        }
                      }
                    });
                    observer.observe(document.documentElement || document.body, {
                      subtree: true,
                      childList: true,
                      characterData: true,
                      attributes: true,
                      attributeFilter: ["class", "style", "hidden", "aria-hidden", "aria-live"],
                    });
                    const selectors = "[role='alert'],[role='status'],[aria-live]:not([aria-live='off'])";
                    const visibleSemanticElements = Array.from(document.querySelectorAll(selectors)).filter(isVisible);
                    const textForElement = (element) => normalize(
                      element instanceof HTMLElement && typeof element.innerText === "string"
                        ? element.innerText
                        : element.textContent || ""
                    );
                    const visibleSemanticTexts = visibleSemanticElements.map(textForElement);
                    if (scanInitialVisibleState) {
                      const staleVisibleTexts = new Set(previousState?.visibleSemanticTexts || []);
                      for (const element of visibleSemanticElements) {
                        if (!staleVisibleTexts.has(textForElement(element))) emit(element);
                      }
                    }
                    window[key] = { observer, bindingName, visibleSemanticTexts };
                    try { previousState?.observer?.disconnect?.(); } catch (e) {}
                  } catch (error) {
                    try { observer?.disconnect?.(); } catch (e) {}
                    throw error;
                  }
                }
                """,
                arg={
                    "bindingName": TRANSIENT_TEXT_BINDING_NAME,
                    "stateKey": TRANSIENT_TEXT_OBSERVER_STATE_KEY,
                    "minLength": TRANSIENT_TEXT_MIN_LENGTH,
                    "maxLength": TRANSIENT_TEXT_MAX_LENGTH,
                    "scanInitialVisibleState": scan_initial_visible_state,
                },
            )
            self._binding_state = binding_state
            self._document_install_success_count += 1
            LOG.info(
                "Transient page text observer installed",
                **self._log_context,
                document_scope="main_frame",
                document_install_attempt_count=self._document_install_attempt_count,
                document_install_success_count=self._document_install_success_count,
            )
        except Exception:
            if binding_state.active_observer is self:
                binding_state.active_observer = previous_active_observer
            LOG.warning(
                "Failed to start transient page text observer",
                **self._log_context,
                document_scope="main_frame",
                document_install_attempt_count=self._document_install_attempt_count,
                exc_info=True,
            )

    async def stop(self) -> None:
        binding_state = self._binding_state
        if binding_state is None:
            return
        if binding_state.active_observer is self:
            try:
                await SkyvernFrame.evaluate(
                    frame=self.page,
                    expression="""
                    ({ bindingName, stateKey }) => {
                      const state = window[stateKey];
                      if (state?.bindingName === bindingName) {
                        state.observer?.disconnect?.();
                        delete window[stateKey];
                      }
                    }
                    """,
                    arg={"bindingName": TRANSIENT_TEXT_BINDING_NAME, "stateKey": TRANSIENT_TEXT_OBSERVER_STATE_KEY},
                )
            except Exception:
                LOG.warning("Failed to stop transient page text observer", **self._log_context, exc_info=True)
            finally:
                binding_state.active_observer = None
        self._binding_state = None
        LOG.info(
            "Transient page text observer stopped",
            **self._log_context,
            document_scope="main_frame",
            document_install_attempt_count=self._document_install_attempt_count,
            document_install_success_count=self._document_install_success_count,
            accepted_event_count=len(self.events),
            elapsed_seconds=time.monotonic() - self._started_at,
        )


def match_user_defined_errors_from_transient_text(
    task: Task,
    step: Step,
    observed_text_events: list[dict[str, Any]],
) -> list[UserDefinedError]:
    if not task.error_code_mapping or not observed_text_events:
        return []

    observed_texts = [
        text
        for event in observed_text_events
        if isinstance(text := event.get("text"), str) and len(text) >= TRANSIENT_TEXT_MATCH_MIN_LENGTH
    ]
    if not observed_texts:
        return []

    normalized_observed_texts = [_normalize_for_match(text) for text in observed_texts]
    matched_errors: list[UserDefinedError] = []
    for error_code, error_description in task.error_code_mapping.items():
        normalized_code = error_code.casefold()
        # Restrict code matching to machine-style codes; natural words are too collision-prone in page text.
        code_pattern = re.compile(rf"\b{re.escape(normalized_code)}\b") if "_" in error_code else None
        normalized_description = _normalize_for_match(error_description) if isinstance(error_description, str) else ""
        matched_texts = [
            observed_texts[index]
            for index, observed_text in enumerate(normalized_observed_texts)
            if (code_pattern is not None and code_pattern.search(observed_text) is not None)
            or (
                normalized_description
                and (
                    normalized_description in observed_text
                    or _has_meaningful_text_overlap(observed_text, normalized_description)
                )
            )
        ]
        if matched_texts:
            matched_errors.append(
                UserDefinedError(
                    error_code=error_code,
                    reasoning=f"Observed transient text during browser wait: {_format_observed_text_reasoning(matched_texts)}",
                    confidence_float=TRANSIENT_TEXT_MATCH_CONFIDENCE,
                )
            )

    if matched_errors:
        if len(matched_errors) > 1:
            LOG.warning(
                "Multiple user-defined error mappings matched transient browser text; using first match",
                task_id=task.task_id,
                step_id=step.step_id,
                matched_error_codes=[error.error_code for error in matched_errors],
                selected_error_code=matched_errors[0].error_code,
                tie_breaker="task.error_code_mapping_order",
            )
        # If multiple mappings match, preserve task.error_code_mapping order so user-authored priority is deterministic.
        return [matched_errors[0]]
    return []
