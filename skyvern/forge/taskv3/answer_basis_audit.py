"""Check the answers Task V3 entered before a click that may move the form on, through a supplied audit.

The audit decides which recorded answers have no basis in the run's data; this module only records what was
answered and refuses every moving action while a flagged answer is unchanged, so the model switches it to a decline
option, clears it or stops. With no audit supplied (the default), nothing is wrapped.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import structlog

from skyvern.forge.taskv3.loop import (
    CODE_TOOL_NAME,
    OPENED_LIST_ERROR_CLASSES,
    PICKED_OPTION_DATA_KEY,
    TARGET_KIND_DATA_KEY,
    TARGET_LABEL_DATA_KEY,
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


@dataclass(frozen=True)
class RecordedAnswer:
    question: str
    value: str


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


def apply_answer_basis_audit(tools: list[ToolSpec], audit: AnswerBasisAudit | None) -> None:
    if audit is None:
        return
    # Each answer's audit starts when it is recorded, so the call overlaps the model's next turns instead of delaying
    # the moving click.
    pending: dict[Field, tuple[str, asyncio.Task[list[RecordedAnswer]]]] = {}
    flagged: dict[Field, str] = {}
    open_menu: dict[str, Field | None] = {"field": None}

    def record(field: Field, value: str) -> None:
        previous = pending.pop(field, None)
        if previous is not None:
            previous[1].cancel()
        # An emptied field holds no answer, so its flag goes with it.
        flagged.pop(field, None)
        if field[0] and value:
            pending[field] = (value, asyncio.create_task(audit.unsupported([RecordedAnswer(field[0], value)])))

    async def hold(spec: ToolSpec, args: dict[str, Any]) -> bool:
        """Refuse the moving action while a flagged answer is unchanged; True when it passes as that field's repair."""
        while pending:
            field, (value, task) = pending.popitem()
            try:
                found = await task
            except Exception:
                LOG.warning("taskv3 answer basis audit raised; not blocking", exc_info=True)
                found = []
            if found:
                flagged[field] = value
        if not flagged:
            return False
        # A click cannot be told from a submit before it lands, so only the flagged field's own control, or a row of an
        # open list, goes through: that is the repair. A ref or mark is compared as the field was recorded, resolved.
        if spec.name == "click":
            selector, menu_row = await spec.address_probe(args) if spec.address_probe else (_selector(args), False)
            if menu_row or any(selector == field[1] for field in flagged):
                return True
        LOG.info("taskv3 answer basis hold", tool=spec.name, flagged=len(flagged))
        named = "; ".join(f"'{q}' answered '{v}' (field {s})" for (q, s), v in flagged.items())
        raise ToolRefusal(
            f"Not done: nothing in the data supports these answers: {named}. For each, choose the field's decline "
            "option, such as prefer not to say, if it offers one. Otherwise, if the question is required, stop, naming "
            "it; if it is optional, clear it and continue. Until each is changed, only a click on that field or on a "
            "row of its open list goes through.",
            error_class="answer_without_basis",
        )

    for spec in tools:
        if spec.name not in ("click", "type", "select_option", "select_combobox", "press_key", CODE_TOOL_NAME):
            continue

        async def wrapped(
            args: dict[str, Any],
            _handler: Callable[[dict[str, Any]], Awaitable[ToolResult]] = spec.handler,
            _spec: ToolSpec = spec,
        ) -> ToolResult:
            _tool = _spec.name
            repair = _may_move_on(_tool, args) and await hold(_spec, args)
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
            field = (_label(result), _selector(args))
            if _tool == "select_combobox":
                record(field, str(args.get("value") or ""))
            elif _tool == "select_option":
                record(field, str(args.get("label") or args.get("value") or ""))
            elif _tool == "type" and str(data.get(TARGET_KIND_DATA_KEY) or "") in _FREE_TEXT_KINDS:
                text = str(args.get("text") or "")
                if len(text.split()) <= _MAX_AUDITED_WORDS:
                    record(field, text)
            return result

        spec.handler = wrapped
