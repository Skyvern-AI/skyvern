from typing import Any

import pytest
from structlog.testing import capture_logs

from skyvern.forge.taskv3.answer_basis_audit import RecordedAnswer, apply_answer_basis_audit
from skyvern.forge.taskv3.loop import (
    CODE_TOOL_NAME,
    PICKED_OPTION_DATA_KEY,
    TARGET_KIND_DATA_KEY,
    TARGET_LABEL_DATA_KEY,
    TARGET_LABEL_IS_PLACEHOLDER_DATA_KEY,
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


async def _answer(args: dict[str, Any]) -> tuple[str, str, str, bool]:
    # What the field reads as before the call acts; `_probed` stands in for a probe that resolves elsewhere or fails.
    if args.get("_probed") == "":
        return "", "", "", False
    selector, label = str(args.get("_probed", args.get("selector"))), str(args.get("_label", ""))
    return selector, label, args.get("_kind", "textbox"), bool(args.get("_placeholder"))


def _tools(dispatched: list[tuple[str, dict[str, Any]]]) -> list[ToolSpec]:
    def make(name: str) -> ToolSpec:
        async def handler(args: dict[str, Any]) -> ToolResult:
            dispatched.append((name, args))
            sel = str(args.get("selector") or "")
            if args.get("_fail"):
                return ToolResult("error", f"{name} failed", {}, error_class="not_editable")
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
            data = {TARGET_LABEL_DATA_KEY: label, TARGET_KIND_DATA_KEY: "textbox"}
            if "_shown" in args:
                data[PICKED_OPTION_DATA_KEY] = args["_shown"]
            if args.get("_placeholder"):
                data[TARGET_LABEL_IS_PLACEHOLDER_DATA_KEY] = True
            return ToolResult("ok", f"{name} done", data)

        probe = _answer if name == "type" else None
        return ToolSpec(
            name=name, description="", parameters={}, handler=handler, address_probe=_address, answer_probe=probe
        )

    names = ("click", "drag", "type", "select_option", "select_combobox", "press_key", CODE_TOOL_NAME, "observe")
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
    # Neither a resubmit, the next question's opener, a submit by ref, an Enter, a drag nor the code tool spends the
    # hold: each is refused and Submit stays refused after them.
    await _held(tools, "click", submit)
    await _held(tools, "click", {"selector": "#q2", "_label": "Are you willing to relocate?"})
    await _held(tools, "click", {"selector": "ref=40"})
    await _held(tools, "press_key", {"key": "Enter"})
    await _held(tools, "drag", {"selector": "#slider", "target": "#end"})
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


@pytest.mark.parametrize("log_only", [False, True])
def test_no_audit_leaves_every_handler_untouched(log_only: bool) -> None:
    # The kill switch resolves the audit to None, which wins over the log-only arm.
    tools = _tools([])
    before = [t.handler for t in tools]
    apply_answer_basis_audit(tools, None, log_only=log_only)
    assert [t.handler for t in tools] == before


@pytest.mark.asyncio
async def test_log_only_would_hold_exactly_the_calls_the_blocking_audit_refuses() -> None:
    submit = {"selector": "#submit"}
    sequence: list[tuple[str, dict[str, Any]]] = [
        ("click", {"selector": "#q1"}),
        ("click", {"selector": '[data-tv3-menu="2"]', "_row": "No"}),
        ("click", submit),
        ("click", {"selector": "ref=40"}),
        ("press_key", {"key": "Enter"}),
        ("drag", {"selector": "#slider", "target": "#end"}),
        (CODE_TOOL_NAME, {"code": "await page.click('#submit')"}),
        ("click", {"selector": "#q1"}),
        ("click", {"selector": "ref=7", "_row": "I prefer not to answer"}),
        ("click", submit),
        # The flagged answer typed again with its own Enter: the log-only arm lets it land, and it stays flagged.
        ("type", {"selector": "#work-auth", "text": "No", "_label": "Are you legally authorized to work?"}),
        (
            "type",
            {
                "selector": "#work-auth",
                "text": "No",
                "press_enter": True,
                "_label": "Are you legally authorized to work?",
            },
        ),
        ("click", submit),
        # An Enter appended with no text leaves the flagged answer in the field.
        ("type", {"selector": "#work-auth", "text": "No", "_label": "Are you legally authorized to work?"}),
        (
            "type",
            {
                "selector": "#work-auth",
                "text": "",
                "clear": False,
                "press_enter": True,
                "_label": "Are you legally authorized to work?",
            },
        ),
        ("click", submit),
    ]

    blocking = _tools([])
    apply_answer_basis_audit(blocking, _Audit(flag={"No"}))
    refused = []
    for name, args in sequence:
        try:
            await _by_name(blocking, name).handler(dict(args))
            refused.append(False)
        except ToolRefusal:
            refused.append(True)

    dispatched: list[tuple[str, dict[str, Any]]] = []
    logged = _tools(dispatched)
    apply_answer_basis_audit(logged, _Audit(flag={"No"}), log_only=True)
    would_hold = []
    for name, args in sequence:
        with capture_logs() as logs:
            await _by_name(logged, name).handler(dict(args))
        would_hold.append(any(e.get("would_hold") for e in logs if e["event"] == "taskv3 answer basis hold"))

    assert refused == would_hold and any(refused) and not all(refused)
    assert refused[-2:] == [True, True]
    assert len(dispatched) == len(sequence)


_APPENDED = "Are you legally authorized to work?"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "clear", "press_enter", "expected"),
    [
        ("", False, True, [False, True, True]),
        (" ", False, True, [False, True, True]),
        ("\t", False, True, [False, True, True]),
        ("\n", False, True, [False, True, True]),
        (" \t\n ", False, True, [False, True, True]),
        ("", False, False, [False, False, True]),
        # The blocking arm lets the submit through with the flagged answer still in the field.
        (" ", False, False, None),
        # A replacement the audit supports drops the flag.
        ("I prefer not to answer", True, True, [False, False, False]),
    ],
)
async def test_log_only_follows_the_blocking_arm_through_every_append_that_adds_no_text(
    text: str, clear: bool, press_enter: bool, expected: list[bool] | None
) -> None:
    field = {"selector": "#work-auth", "_label": _APPENDED}
    sequence: list[tuple[str, dict[str, Any]]] = [
        ("type", {**field, "text": "No"}),
        ("type", {**field, "text": text, "clear": clear, "press_enter": press_enter}),
        ("click", {"selector": "#submit"}),
    ]

    blocking = _tools([])
    apply_answer_basis_audit(blocking, _Audit(flag={"No"}))
    refused = []
    for name, args in sequence:
        try:
            await _by_name(blocking, name).handler(dict(args))
            refused.append(False)
        except ToolRefusal:
            refused.append(True)

    logged = _tools([])
    apply_answer_basis_audit(logged, _Audit(flag={"No"}), log_only=True)
    would_hold = []
    for name, args in sequence:
        with capture_logs() as logs:
            await _by_name(logged, name).handler(dict(args))
        would_hold.append(any(e.get("would_hold") for e in logs if e["event"] == "taskv3 answer basis hold"))

    assert would_hold == refused
    if expected is not None:
        assert refused == expected


WORK_AUTH = "Are you legally authorized to work in the United States?"
TYPED = {"selector": "#work-auth", "text": "No", "press_enter": True, "_label": WORK_AUTH}


@pytest.mark.asyncio
async def test_an_answer_typed_with_enter_is_judged_before_the_call_acts() -> None:
    dispatched: list[tuple[str, dict[str, Any]]] = []
    tools = _tools(dispatched)
    audit = _Audit(flag={"No"})
    apply_answer_basis_audit(tools, audit)

    # The call's own Enter could submit the form, so a flagged answer never reaches the page, and nothing is recorded
    # for a value the field never held.
    message = await _held(tools, "type", dict(TYPED))
    assert f"'{WORK_AUTH}' with 'No' (field #work-auth; not typed" in message
    assert dispatched == [] and audit.seen == [[RecordedAnswer(WORK_AUTH, "No")]]
    # Retrying the refused answer is not a second chance at the judge.
    audit.flag = set()
    await _held(tools, "type", dict(TYPED))
    assert dispatched == [] and len(audit.seen) == 1
    audit.flag = {"No"}
    await _by_name(tools, "click").handler({"selector": "#next"})

    # A supported answer goes through once, with the call's own arguments, and is not audited a second time.
    supported = {**TYPED, "text": "Yes"}
    await _by_name(tools, "type").handler(dict(supported))
    await _by_name(tools, "click").handler({"selector": "#submit"})
    assert [args for _, args in dispatched] == [{"selector": "#next"}, supported, {"selector": "#submit"}]
    assert len(audit.seen) == 2


@pytest.mark.asyncio
async def test_a_typed_enter_that_does_not_land_leaves_the_fields_flag_in_place() -> None:
    dispatched: list[tuple[str, dict[str, Any]]] = []
    tools = _tools(dispatched)
    audit = _Audit(flag={"No"})
    apply_answer_basis_audit(tools, audit)
    type_ = _by_name(tools, "type").handler
    await type_({**TYPED, "press_enter": False})
    await _held(tools, "click", {"selector": "#submit"})

    # Typing the held value again is not a second chance at the judge.
    audit.flag = set()
    await _held(tools, "type", dict(TYPED))
    audit.flag = {"No"}

    # A supported answer whose call fails, and an empty one, change nothing on the page: the held "No" still holds.
    await type_({**TYPED, "text": "Yes", "_fail": True})
    for unchanged in ("", " because of my visa"):
        message = await _held(tools, "type", {**TYPED, "text": unchanged, "clear": False})
        assert "answered 'No'" in message
    message = await _held(tools, "type", {**TYPED, "text": ""})
    assert "answered 'No'" in message
    await _held(tools, "click", {"selector": "#submit"})
    # A supported answer that lands replaces the held one, so the submit goes through.
    await type_({**TYPED, "text": "Yes"})
    await _by_name(tools, "click").handler({"selector": "#submit"})
    assert dispatched[-1] == ("click", {"selector": "#submit"})

    # A typed answer the audit never reads (here a password-like field) is entered as before, unjudged.
    tools = _tools(dispatched)
    audit = _Audit(flag={"No"})
    apply_answer_basis_audit(tools, audit)
    await _by_name(tools, "type").handler({**TYPED, "selector": "#pin", "_kind": "password", "_label": "PIN"})
    assert [args.get("selector") for _, args in dispatched][-1] == "#pin" and audit.seen == []


@pytest.mark.asyncio
async def test_an_answer_appended_with_enter_into_an_empty_field_is_judged_before_the_call_acts() -> None:
    dispatched: list[tuple[str, dict[str, Any]]] = []
    tools = _tools(dispatched)
    apply_answer_basis_audit(tools, _Audit(flag={"No"}))

    assert "not typed" in await _held(tools, "type", {**TYPED, "clear": False})
    assert dispatched == []


@pytest.mark.asyncio
async def test_a_typed_enter_the_probe_cannot_place_is_still_recorded_after_the_call() -> None:
    # Unread: the call acts as it did before this check, and its answer is recorded from the result and held.
    dispatched: list[tuple[str, dict[str, Any]]] = []
    tools = _tools(dispatched)
    audit = _Audit(flag={"No"})
    apply_answer_basis_audit(tools, audit)
    await _by_name(tools, "type").handler({**TYPED, "_probed": ""})
    await _held(tools, "click", {"selector": "#submit"})
    assert [name for name, _ in dispatched] == ["type"] and len(audit.seen) == 1

    # Read as another field: judged before, and recorded again as the field the call reports.
    dispatched.clear()
    tools = _tools(dispatched)
    audit = _Audit(flag={"No"})
    apply_answer_basis_audit(tools, audit)
    await _by_name(tools, "type").handler({**TYPED, "_probed": "#elsewhere", "text": "Yes"})
    await _by_name(tools, "click").handler({"selector": "#submit"})
    assert [name for name, _ in dispatched] == ["type", "click"] and len(audit.seen) == 2


@pytest.mark.asyncio
async def test_the_audit_records_what_the_page_shows_and_where_its_question_came_from() -> None:
    audit = _Audit()
    tools = _tools([])
    apply_answer_basis_audit(tools, audit)
    select, type_ = _by_name(tools, "select_option").handler, _by_name(tools, "type").handler

    await select({"selector": "#auth", "value": "10", "_label": DEBARMENT, "_shown": "No"})
    # A field named only by its placeholder is still audited; the audit is told the name is a placeholder.
    await type_({"selector": "#country", "text": "Canada", "_label": "Country", "_placeholder": True})
    await type_({"selector": "#visa", "text": "No", "_label": "Visa", "_placeholder": True, "press_enter": True})
    await _by_name(tools, "click").handler({"selector": "#submit"})

    assert audit.seen == [
        [RecordedAnswer(DEBARMENT, "No")],
        [RecordedAnswer("Country", "Canada", label_is_placeholder=True)],
        [RecordedAnswer("Visa", "No", label_is_placeholder=True)],
    ]
