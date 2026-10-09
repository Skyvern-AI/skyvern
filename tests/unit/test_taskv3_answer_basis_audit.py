from typing import Any

import pytest
from structlog.testing import capture_logs

from skyvern.forge.taskv3.answer_basis_audit import RecordedAnswer, apply_answer_basis_audit
from skyvern.forge.taskv3.loop import (
    CODE_TOOL_NAME,
    PICKED_OPTION_DATA_KEY,
    TARGET_KIND_DATA_KEY,
    TARGET_LABEL_DATA_KEY,
    ToolRefusal,
    ToolResult,
    ToolSpec,
)

DEBARMENT = "Have you ever been excluded from a federal program?"


class _Audit:
    def __init__(self, flag: set[str] | None = None, fail: bool = False) -> None:
        self.flag = flag or set()
        self.fail = fail
        self.seen: list[list[RecordedAnswer]] = []

    async def unsupported(self, answers: list[RecordedAnswer]) -> list[RecordedAnswer]:
        self.seen.append(answers)
        if self.fail:
            raise TimeoutError
        return [a for a in answers if a.value in self.flag]


# What each ref resolves to, and whether it is a row of an open list.
_REFS = {"ref=7": ('[data-tv3="7"]', True), "ref=30": ("#q1", False), "ref=40": ("#submit", False)}


async def _address(args: dict[str, Any]) -> tuple[str, bool]:
    selector = str(args.get("selector") or "")
    return _REFS.get(selector, (selector, False))


def _tools(dispatched: list[tuple[str, dict[str, Any]]]) -> list[ToolSpec]:
    def make(name: str) -> ToolSpec:
        async def handler(args: dict[str, Any]) -> ToolResult:
            dispatched.append((name, args))
            sel = str(args.get("selector") or "")
            if name == "click" and "_row" in args:
                return ToolResult("ok", f"clicked {sel}", {PICKED_OPTION_DATA_KEY: args["_row"]})
            if name == "click" and sel.startswith("#q"):
                # A question's opener: its menu opens.
                data = {TARGET_LABEL_DATA_KEY: args.get("_label", DEBARMENT), "menu_note": "listed"}
                return ToolResult("ok", f"clicked {sel}", data)
            label = args.get("_label", "")
            if name == "select_combobox" and args.get("_no_match"):
                content = f"opened {sel} but no option matched {args['value']!r}"
                return ToolResult("error", content, {TARGET_LABEL_DATA_KEY: label}, error_class="list_no_match")
            return ToolResult("ok", f"{name} done", {TARGET_LABEL_DATA_KEY: label, TARGET_KIND_DATA_KEY: "textbox"})

        return ToolSpec(name=name, description="", parameters={}, handler=handler, address_probe=_address)

    names = ("click", "type", "select_option", "select_combobox", "press_key", CODE_TOOL_NAME, "observe")
    return [make(n) for n in names]


def _by_name(tools: list[ToolSpec], name: str) -> ToolSpec:
    return next(t for t in tools if t.name == name)


async def _held(tools: list[ToolSpec], name: str, args: dict[str, Any]) -> str:
    with pytest.raises(ToolRefusal) as held:
        await _by_name(tools, name).handler(args)
    assert held.value.error_class == "answer_without_basis"
    return str(held.value)


@pytest.mark.asyncio
async def test_an_unsupported_answer_holds_every_moving_action_until_it_is_replaced() -> None:
    dispatched: list[tuple[str, dict[str, Any]]] = []
    tools = _tools(dispatched)
    apply_answer_basis_audit(tools, _Audit(flag={"No"}))
    click = _by_name(tools, "click").handler
    submit = {"selector": "#submit"}

    # A menu pick is recorded against the question whose menu it closed.
    await click({"selector": "#q1"})
    await click({"selector": '[data-tv3-menu="2"]', "_row": "No"})
    message = await _held(tools, "click", submit)
    assert DEBARMENT in message and "#q1" in message
    # Neither a resubmit, the next question's opener, a submit by ref, an Enter nor the code tool spends the hold: each
    # is refused and Submit stays refused after them.
    await _held(tools, "click", submit)
    await _held(tools, "click", {"selector": "#q2", "_label": "Are you willing to relocate?"})
    await _held(tools, "click", {"selector": "ref=40"})
    await _held(tools, "press_key", {"key": "Enter"})
    await _held(tools, CODE_TOOL_NAME, {"code": "await page.click('#submit')"})
    await _held(tools, "click", submit)
    assert [args for name, args in dispatched if name == "click"].count(submit) == 0

    # Repair: the flagged field's own opener and a ref pick of a list row go through.
    with capture_logs() as logs:
        await click({"selector": "#q1"})
        await click({"selector": "ref=7", "_row": "I prefer not to answer"})
    assert [e["kind"] for e in logs if e["event"] == "taskv3 answer basis hold passed"] == ["repair", "repair"]
    await click(submit)
    assert ("click", submit) in dispatched

    # Picking the held value again brings the hold back.
    await click({"selector": "#q1"})
    await click({"selector": '[data-tv3-menu="2"]', "_row": "No"})
    await _held(tools, "click", submit)


@pytest.mark.asyncio
async def test_a_click_let_through_as_a_repair_that_picks_nothing_is_logged_as_a_bypass() -> None:
    tools = _tools([])
    apply_answer_basis_audit(tools, _Audit(flag={"No"}))
    click = _by_name(tools, "click").handler
    await click({"selector": "#q1"})
    await click({"selector": '[data-tv3-menu="2"]', "_row": "No"})
    await click({"selector": "#q1"})
    with capture_logs() as logs:
        await click({"selector": "ref=30"})
    assert [e["kind"] for e in logs if e["event"] == "taskv3 answer basis hold passed"] == ["bypass"]


@pytest.mark.asyncio
async def test_two_fields_with_the_same_label_are_audited_apart() -> None:
    tools = _tools([])
    audit = _Audit(flag={"United States"})
    apply_answer_basis_audit(tools, audit)
    select = _by_name(tools, "select_option").handler
    await select({"selector": "#citizenship-country", "label": "United States", "_label": "Country"})
    await select({"selector": "#address-country", "label": "Canada", "_label": "Country"})

    message = await _held(tools, "click", {"selector": "#submit"})
    assert "'Country' answered 'United States' (field #citizenship-country)" in message
    assert "Canada" not in message


@pytest.mark.asyncio
async def test_the_refusal_stops_only_on_a_required_question_and_a_cleared_answer_lifts_the_hold() -> None:
    dispatched: list[tuple[str, dict[str, Any]]] = []
    tools = _tools(dispatched)
    apply_answer_basis_audit(tools, _Audit(flag={"Canadian", "Yes"}))
    await _by_name(tools, "type").handler({"selector": "#nat", "text": "Canadian", "_label": "Nationality"})
    await _by_name(tools, "select_combobox").handler({"selector": "#auth", "value": "Yes", "_label": "Authorized?"})

    message = await _held(tools, "click", {"selector": "#next"})
    assert "if the question is required, stop" in message and "if it is optional, clear it and continue" in message
    assert "'Nationality' answered 'Canadian'" in message and "'Authorized?' answered 'Yes'" in message

    await _by_name(tools, "type").handler({"selector": "#nat", "text": "", "_label": "Nationality"})
    await _by_name(tools, "select_combobox").handler({"selector": "#auth", "value": "", "_label": "Authorized?"})
    await _by_name(tools, "click").handler({"selector": "#next"})
    assert ("click", {"selector": "#next"}) in dispatched


@pytest.mark.asyncio
async def test_a_pick_is_credited_to_the_menu_left_open_never_to_an_earlier_one() -> None:
    tools = _tools([])
    audit = _Audit(flag={"Other"})
    apply_answer_basis_audit(tools, audit)
    click = _by_name(tools, "click").handler
    combobox = _by_name(tools, "select_combobox").handler

    await click({"selector": "#q1"})
    await click({"selector": '[data-tv3-menu="1"]', "_row": "I don't need sponsorship"})
    # A select_combobox with no matching row fails but leaves its list open for a pick.
    await combobox({"selector": "ref=11", "value": "United States", "_label": "Nationality", "_no_match": True})
    await click({"selector": '[data-tv3-menu="2"]', "_row": "Other"})
    # A pick with no menu left open is credited to no question.
    await click({"selector": '[data-tv3-menu="1"]', "_row": "Yes"})
    # Return submits like Enter, so it waits for the audits and is held.
    assert "'Nationality' answered 'Other'" in await _held(tools, "press_key", {"key": "Return"})

    recorded = sum(audit.seen, [])
    assert RecordedAnswer(DEBARMENT, "I don't need sponsorship") in recorded
    assert RecordedAnswer("Nationality", "Other") in recorded
    assert RecordedAnswer(DEBARMENT, "Other") not in recorded
    assert all(a.value != "Yes" for a in recorded)


@pytest.mark.asyncio
async def test_reads_add_no_audit_and_an_audit_that_raises_never_blocks() -> None:
    dispatched: list[tuple[str, dict[str, Any]]] = []
    tools = _tools(dispatched)
    audit = _Audit(fail=True)
    apply_answer_basis_audit(tools, audit)
    await _by_name(tools, "select_combobox").handler({"value": "Yes", "_label": "Authorized to work?"})
    # The audit starts when the answer is recorded; a read neither starts nor waits for one.
    await _by_name(tools, "observe").handler({})
    await _by_name(tools, "click").handler({"selector": "#next"})
    assert ("click", {"selector": "#next"}) in dispatched and len(audit.seen) == 1


def test_no_audit_leaves_every_handler_untouched() -> None:
    tools = _tools([])
    before = [t.handler for t in tools]
    apply_answer_basis_audit(tools, None)
    assert [t.handler for t in tools] == before
