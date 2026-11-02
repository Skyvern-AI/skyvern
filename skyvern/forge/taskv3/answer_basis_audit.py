"""Check the answers Task V3 entered before a click that may move the form on, through a supplied audit.

The audit decides which recorded answers have no basis in the run's data; this module only records what was
answered and refuses every moving action while a flagged answer is unchanged, so the model switches it to a decline
option, clears it or stops. The tenth refused call ends the task. With no audit supplied (the default), nothing is
wrapped.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import structlog

from skyvern.forge.taskv3.loop import (
    ANSWER_BASIS_HOLD_BOUND_GUARD,
    CODE_TOOL_NAME,
    GUARD_END_DATA_KEY,
    OPENED_LIST_ERROR_CLASSES,
    PICKED_OPTION_DATA_KEY,
    TARGET_KIND_DATA_KEY,
    TARGET_LABEL_DATA_KEY,
    TARGET_LABEL_IS_PLACEHOLDER_DATA_KEY,
    GuardEnd,
    ToolRefusal,
    ToolResult,
    ToolSpec,
    _may_submit,
)

LOG = structlog.get_logger(__name__)

_MENU_ROW = re.compile(r"data-tv3-menu=")
# The click tool's census of a menu it opened (tools._menu_note_fields).
_MENU_NOTE_KEY = "menu_note"
# Free text only: a typed password, email, URL, phone or number is never sent to the audit.
_FREE_TEXT_KINDS = frozenset({"textbox", "textarea", "combobox"})
# Longer typed text is a cover letter or a description, never a legal-standing answer.
_MAX_AUDITED_WORDS = 25
# The refused call that ends the task (holds count per call, per engine run of one task).
ANSWER_BASIS_MAX_HOLDS = 10


@dataclass(frozen=True)
class RecordedAnswer:
    question: str
    value: str
    # The question is the field's placeholder, the only text it shows; the audit decides whether it asks anything.
    label_is_placeholder: bool = False


class AnswerBasisAudit(Protocol):
    async def unsupported(self, answers: list[RecordedAnswer]) -> list[RecordedAnswer]:
        """The answers to hold: no basis found, or an unreadable verdict; empty when the check could not run."""
        ...


# A field is its question label and the selector it was answered through: two questions can share a label.
Field = tuple[str, str]


def _data(result: ToolResult) -> dict[str, Any]:
    return result.data if isinstance(result.data, dict) else {}


def _label(result: ToolResult) -> str:
    return str(_data(result).get(TARGET_LABEL_DATA_KEY) or "").strip()


def _selector(args: dict[str, Any]) -> str:
    return str(args.get("selector") or "").strip()


def _may_move_on(tool: str, args: dict[str, Any]) -> bool:
    # A click on a listed menu row picks an answer; it moves nothing on.
    return _may_submit(tool, args) and not (tool == "click" and _MENU_ROW.search(_selector(args)))


def apply_answer_basis_audit(
    tools: list[ToolSpec],
    audit: AnswerBasisAudit | None,
    *,
    log_only: bool = False,
    log_fields: Mapping[str, str] | None = None,
) -> None:
    """``log_only`` logs each hold it would make and lets the call through, uncounted; what lands stays flagged."""
    if audit is None:
        return
    extra = log_fields or {}
    # Each answer's audit starts when it is recorded, so the call overlaps the model's next turns instead of delaying
    # the moving click.
    pending: dict[Field, tuple[str, asyncio.Task[list[RecordedAnswer]]]] = {}
    flagged: dict[Field, str] = {}
    refused: set[tuple[Field, str]] = set()
    open_menu: dict[str, Field | None] = {"field": None}
    placeholders: set[Field] = set()
    holds = 0

    def answer(field: Field, value: str) -> RecordedAnswer:
        return RecordedAnswer(field[0], value, field in placeholders)

    def record(field: Field, value: str) -> None:
        previous = pending.pop(field, None)
        if previous is not None:
            previous[1].cancel()
        # An emptied field holds no answer, so its flag goes with it.
        flagged.pop(field, None)
        if field[0] and value:
            pending[field] = (value, asyncio.create_task(audit.unsupported([answer(field, value)])))

    async def found(job: Awaitable[list[RecordedAnswer]]) -> bool:
        try:
            return bool(await job)
        except Exception:
            LOG.warning("taskv3 answer basis audit raised; not blocking", exc_info=True)
            return False

    async def hold(
        spec: ToolSpec, args: dict[str, Any], entering: tuple[Field, str] | None = None, appends: bool = False
    ) -> tuple[bool, dict[Field, str]]:
        """Refuse the moving action while a flagged answer is unchanged; True when it passes as that field's repair."""
        while pending:
            field, (value, task) = pending.popitem()
            if await found(task):
                flagged[field] = value
        shown = dict(flagged)
        untyped = None
        # The answer a call enters is judged as if it had landed, recording nothing, and never twice once refused.
        # Appended text leaves the field's earlier answer, and its flag, in place.
        if entering is not None and flagged.get(entering[0]) != entering[1]:
            if not appends:
                shown.pop(entering[0], None)
            if entering in refused or await found(audit.unsupported([answer(*entering)])):
                refused.add(entering)
                shown[entering[0]] = entering[1]
                untyped = entering[0]
        if not shown:
            return False, {}
        # A click cannot be told from a submit before it lands, so only the flagged field's own control, or a row of an
        # open list, goes through: that is the repair. A ref or mark is compared as the field was recorded, resolved.
        if spec.name == "click":
            selector, menu_row = await spec.address_probe(args) if spec.address_probe else (_selector(args), False)
            if menu_row or any(selector == field[1] for field in flagged):
                return True, {}
        nonlocal holds
        if log_only:
            LOG.info(
                "taskv3 answer basis hold",
                tool=spec.name,
                flagged=len(shown),
                holds=holds,
                log_only=True,
                would_hold=True,
                **extra,
            )
            return False, shown
        holds += 1
        LOG.info("taskv3 answer basis hold", tool=spec.name, flagged=len(shown), holds=holds, **extra)
        named = "; ".join(
            f"'{q}' with '{v}' (field {s}; not typed, nothing was entered or pressed)"
            if (q, s) == untyped
            else f"'{q}' answered '{v}' (field {s})"
            for (q, s), v in shown.items()
        )
        questions = "; ".join(f"'{q}'" for q, _ in shown)
        end = GuardEnd(
            ANSWER_BASIS_HOLD_BOUND_GUARD,
            f"Stopped without submitting: the answer audit found nothing in the data that supports the answer to "
            f"{questions}.",
        )
        raise ToolRefusal(
            f"Not done: nothing in the data supports these answers: {named}. For each, choose the field's decline "
            "option, such as prefer not to say, if it offers one. Otherwise, if the question is required, stop, naming "
            "it; if it is optional, clear it and continue. Until each answered one is changed, only a click on that field "
            "or on a row of its open list goes through.",
            error_class="answer_without_basis",
            data={GUARD_END_DATA_KEY: end} if holds >= ANSWER_BASIS_MAX_HOLDS else None,
        )

    for spec in tools:
        if spec.name not in ("click", "drag", "type", "select_option", "select_combobox", "press_key", CODE_TOOL_NAME):
            continue

        async def wrapped(
            args: dict[str, Any],
            _handler: Callable[[dict[str, Any]], Awaitable[ToolResult]] = spec.handler,
            _spec: ToolSpec = spec,
        ) -> ToolResult:
            _tool = _spec.name
            entering: tuple[Field, str] | None = None
            text = str(args.get("text") or "")
            adds_text = bool(text.strip())
            appends = not args.get("clear", True)
            # Its own Enter can submit the form, so the text is judged first, appended (clear=false) or not.
            enters = args.get("press_enter") and adds_text and len(text.split()) <= _MAX_AUDITED_WORDS
            if _spec.answer_probe is not None and enters:
                selector, label, kind, from_placeholder = await _spec.answer_probe(args)
                if label.strip() and kind in _FREE_TEXT_KINDS:
                    entering = ((label.strip(), selector), text)
                    if from_placeholder:
                        placeholders.add(entering[0])
                elif not selector:
                    LOG.info("taskv3 answer basis probe missed", tool=_tool)
            repair, kept = await hold(_spec, args, entering, appends) if _may_move_on(_tool, args) else (False, {})
            result = await _handler(args)
            data = _data(result)
            picked = str(data.get(PICKED_OPTION_DATA_KEY) or "") if _tool == "click" and result.status == "ok" else ""
            opened = bool(data.get(_MENU_NOTE_KEY)) or result.error_class in OPENED_LIST_ERROR_CLASSES
            if repair:
                # A click let through as a repair that neither opened a list nor picked a row went somewhere else.
                LOG.info("taskv3 answer basis hold passed", kind="repair" if picked or opened else "bypass")
            if picked and open_menu["field"] is not None:
                record(open_menu["field"], picked)
            # A control that opened a list names the open question, an error included (a select_combobox with no
            # match leaves its list open). A pick or any other completed action ends it, so a later pick is never
            # credited to an earlier menu's question; an error that changed nothing leaves it open.
            if opened:
                open_menu["field"] = (_label(result), _selector(args))
            elif result.status == "ok":
                open_menu["field"] = None
            if result.status != "ok" or picked:
                return result
            # The ref resolver rewrites args["selector"] in place, so this is the selector the call acted on.
            field = (_label(result), _selector(args))
            if data.get(TARGET_LABEL_IS_PLACEHOLDER_DATA_KEY):
                placeholders.add(field)
            else:
                placeholders.discard(field)
            if entering is not None and field == entering[0]:
                record(field, "")
                # A flagged answer the log-only arm let land still holds, as the blocking arm would keep holding it.
                if field in kept:
                    flagged[field] = kept[field]
                return result
            # The blocking arm refused this call, so an append that adds no text leaves the flag it holds.
            if field in kept and appends and not adds_text:
                return result
            if _tool == "select_combobox":
                record(field, str(args.get("value") or ""))
            elif _tool == "select_option":
                record(field, str(data.get(PICKED_OPTION_DATA_KEY) or args.get("label") or args.get("value") or ""))
            elif _tool == "type" and str(data.get(TARGET_KIND_DATA_KEY) or "") in _FREE_TEXT_KINDS:
                text = str(args.get("text") or "")
                # Appending nothing leaves the field's answer, and its flag, as they were.
                if (text or args.get("clear", True)) and len(text.split()) <= _MAX_AUDITED_WORDS:
                    record(field, text)
            return result

        spec.handler = wrapped
