"""Unit tests for the native Task V3 engine (prompt + tools + loop assembly).

Reuses the scripted fake LLMCaller from the loop test and the fake Playwright page
from the tools test, so the engine's wiring is exercised without a real LLM or browser.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
import pytest
import yarl

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.taskv3.engine import (
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_MAX_TURNS,
    MAX_TOOL_CALLS_PER_ACTION_STEP,
    MAX_TURNS_PER_ACTION_STEP,
    coerce_v3_parameters,
    run_task_v3_agent_loop,
    taskv3_runaway_backstops,
)
from skyvern.forge.taskv3.loop import ToolResult, ToolSpec
from skyvern.forge.taskv3.opaque_refs import mask_opaque_urls
from skyvern.forge.taskv3.tools import PAGE_UNAVAILABLE_ERROR
from tests.unit.test_taskv3_loop import _ScriptedCaller
from tests.unit.test_taskv3_tools import _FakePage, _fixed_page_provider


@pytest.mark.asyncio
async def test_engine_completes_after_acting() -> None:
    # observe -> type -> finish(completed): the first finish is accepted (no forced extra turn).
    script = [
        [("observe", {})],
        [("type", {"selector": "#first", "text": "John"})],
        [("finish", {"status": "completed", "reason": "filled, ready to submit"})],
    ]
    caller = _ScriptedCaller(script)
    page = _FakePage()
    outcome = await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(page),
        llm_caller=caller,
        goal="Fill the application form and stop before submitting.",
        parameters={"first_name": "John"},
        starting_url="https://example.test/apply",
    )
    assert outcome.status == "completed"
    assert outcome.reason == "filled, ready to submit"
    assert outcome.turns == 3
    # The fill actually dispatched to the page.
    assert any(c[0] == "fill" and c[1]["selector"] == "#first" for c in page.calls)


@pytest.mark.asyncio
async def test_engine_accepts_first_finish() -> None:
    script = [[("finish", {"status": "completed", "reason": "done"})]]
    caller = _ScriptedCaller(script)
    outcome = await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()), llm_caller=caller, goal="noop"
    )
    assert outcome.status == "completed" and outcome.turns == 1


@pytest.mark.asyncio
async def test_navigate_through_a_payload_ref_redirect_masks_the_landing_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A payload ref that redirects hands its provenance to the landing URL: the real navigate tool
    derives a ref for it, the engine's context holds the same dict, and the boundary masks it."""
    import skyvern.utils.url_validators as urlv

    monkeypatch.setattr(urlv, "validate_fetch_url", lambda url: url)  # no DNS in unit tests
    signed = "https://files.example.test/uploads/deadbeef/resume.pdf?token=eyJhbGciOiJIUzI1NiJ9.c2lnbmVk.QQ"
    landing = "https://cdn.example.test/blob/resume.pdf?X-Amz-Signature=0123456789abcdef0123456789abcdef"

    class _RedirectingPage(_FakePage):
        async def goto(self, url: str, timeout: int | None = None, wait_until: str | None = None) -> None:
            await super().goto(url, timeout, wait_until)
            self.url = landing

    token = mask_opaque_urls({"file": signed}).masked["file"]
    caller = _ScriptedCaller([[("navigate", {"url": token})], [("finish", {"status": "completed", "reason": "done"})]])
    ctx = SkyvernContext(task_id="tsk_redirect")
    skyvern_context.set(ctx)
    try:
        await run_task_v3_agent_loop(
            page_provider=_fixed_page_provider(_RedirectingPage()),
            llm_caller=caller,
            goal="g",
            parameters={"file": signed},
        )
    finally:
        skyvern_context.reset()
    navigate_message = next(m["content"] for m in caller.message_history if m.get("role") == "tool")
    assert "0123456789abcdef" not in navigate_message and navigate_message.startswith("navigated to opaque_url_")
    assert landing in ctx.opaque_url_refs.values()


@pytest.mark.asyncio
async def test_engine_overwrites_opaque_url_refs_so_a_prior_blocks_refs_never_bleed() -> None:
    """The masking boundary reads ctx.opaque_url_refs, and one SkyvernContext is shared across every
    task block in a workflow run. The engine must OVERWRITE that field with the current task's refs —
    the minted set, or empty when the task mints none — never merge or leave a prior block's stale
    entry. Otherwise a later block masks a URL to a token only the earlier block's resolver can
    reverse, which the model then cannot round-trip back through a tool call."""
    signed = "https://files.example.test/uploads/deadbeef/resume.pdf?token=eyJhbGciOiJIUzI1NiJ9.c2lnbmVk.QQ"
    ctx = SkyvernContext(task_id="tsk_prior")
    ctx.opaque_url_refs = {"opaque_url_stale00": "https://old.example.test/x?token=STALE"}
    skyvern_context.set(ctx)
    try:
        await run_task_v3_agent_loop(
            page_provider=_fixed_page_provider(_FakePage()),
            llm_caller=_ScriptedCaller([[("finish", {"status": "completed", "reason": "done"})]]),
            goal="g",
            parameters={"file": signed},
        )
        # Overwritten with exactly this task's refs; the prior block's stale entry is gone.
        assert ctx.opaque_url_refs == mask_opaque_urls({"file": signed}).refs
        assert "opaque_url_stale00" not in ctx.opaque_url_refs

        await run_task_v3_agent_loop(
            page_provider=_fixed_page_provider(_FakePage()),
            llm_caller=_ScriptedCaller([[("finish", {"status": "completed", "reason": "done"})]]),
            goal="g",
            parameters={"first_name": "John"},
        )
        # A task that mints no refs resets the field to empty, not the previous task's refs.
        assert ctx.opaque_url_refs == {}
    finally:
        skyvern_context.reset()


@pytest.mark.asyncio
async def test_engine_terminate_accepted_immediately() -> None:
    script = [[("finish", {"status": "terminated", "reason": "CAPTCHA blocks the form"})]]
    caller = _ScriptedCaller(script)
    outcome = await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()), llm_caller=caller, goal="apply"
    )
    assert outcome.status == "terminated" and outcome.turns == 1


@pytest.mark.asyncio
async def test_engine_exposes_browser_and_finish_tools_no_task_ecosystem() -> None:
    caller = _ScriptedCaller([[("finish", {"status": "completed", "reason": "x"})]])
    await run_task_v3_agent_loop(page_provider=_fixed_page_provider(_FakePage()), llm_caller=caller, goal="x")
    sent = {t["function"]["name"] for t in (caller.sent_tools or [])}
    assert {"observe", "type", "click", "file_upload", "finish"} <= sent
    assert not ({"act", "extract", "validate", "run_task", "login"} & sent)


@pytest.mark.asyncio
async def test_engine_records_billable_actions() -> None:
    # observe/finish are not billable; type + click are — so per-action billing counts 2.
    script = [
        [("observe", {})],
        [("type", {"selector": "#first", "text": "John"})],
        [("click", {"selector": "#submit"})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]
    caller = _ScriptedCaller(script)
    outcome = await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()), llm_caller=caller, goal="apply"
    )
    assert outcome.status == "completed"
    assert outcome.billable_actions == ["type", "click"]


@pytest.mark.asyncio
async def test_engine_wires_budget_and_retry_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # The engine must pass real cost ceilings + transient-retry policy to the loop by default,
    # so the wired path (which passes neither) inherits them. Pins the defaults against regression.
    from skyvern.forge.sdk.api.llm.exceptions import LLMProviderErrorRetryableTask
    from skyvern.forge.taskv3 import engine as engine_mod
    from skyvern.forge.taskv3.loop import LoopOutcome

    captured: dict[str, object] = {}

    async def _capture(**kwargs: object) -> LoopOutcome:
        captured.update(kwargs)
        return LoopOutcome(status="completed", reason="ok")

    monkeypatch.setattr(engine_mod, "run_agent_tool_loop", _capture)
    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()), llm_caller=_ScriptedCaller([]), goal="x"
    )

    assert captured["max_tokens"] == engine_mod.DEFAULT_MAX_TOKENS
    assert captured["deadline_seconds"] == engine_mod.DEFAULT_DEADLINE_SECONDS
    assert captured["max_call_retries"] == engine_mod.DEFAULT_MAX_CALL_RETRIES
    assert captured["retryable_call_exceptions"] == (LLMProviderErrorRetryableTask,)


def test_runaway_backstops_scale_with_action_step_budget() -> None:
    # No action-step budget -> the guards are the engine's fixed defaults.
    assert taskv3_runaway_backstops(None) == (DEFAULT_MAX_TURNS, DEFAULT_MAX_TOOL_CALLS)
    assert taskv3_runaway_backstops(0) == (DEFAULT_MAX_TURNS, DEFAULT_MAX_TOOL_CALLS)
    # Small cap: the fixed floors dominate, so a productive run keeps its historical headroom.
    assert taskv3_runaway_backstops(10) == (DEFAULT_MAX_TURNS, DEFAULT_MAX_TOOL_CALLS)
    # Large cap: both guards scale up so the action-step budget -- not the guards -- bounds the run.
    big = 100
    assert taskv3_runaway_backstops(big) == (
        big * MAX_TURNS_PER_ACTION_STEP,
        big * MAX_TOOL_CALLS_PER_ACTION_STEP,
    )
    # Monotonic: a larger cap never yields smaller guards.
    t_small, c_small = taskv3_runaway_backstops(20)
    t_big, c_big = taskv3_runaway_backstops(80)
    assert t_big >= t_small and c_big >= c_small


@pytest.mark.asyncio
async def test_engine_page_lost_fails_cleanly_not_hang() -> None:
    # A provider that never resolves a page (browser truly gone): every browser tool call errors
    # with a browser-lost reason, and the loop's existing action-step/turn backstops guarantee a
    # bounded, clean failure -- no hang, and no new termination mechanism was needed for it.
    async def gone_provider() -> Any:
        return None

    script = [[("click", {"selector": "#x"})]] * 10  # keeps retrying; would never finish on its own
    caller = _ScriptedCaller(script)
    outcome = await run_task_v3_agent_loop(
        page_provider=gone_provider,
        llm_caller=caller,
        goal="apply",
        max_action_steps=2,
        max_turns=20,
    )
    assert outcome.status == "budget_exhausted"
    tool_messages = [m for m in outcome.messages if m.get("role") == "tool"]
    assert any(m["content"] == PAGE_UNAVAILABLE_ERROR for m in tool_messages)


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"full_name": "Ada", "email": "a@x.test"}, {"full_name": "Ada", "email": "a@x.test"}),
        # JSON object stored as a string (single-encoded): parsed so the profile reaches the model
        # instead of being dropped to None by an isinstance(dict) check (the org-at-0% regression).
        ('{"full_name": "Ada", "email": "a@x.test"}', {"full_name": "Ada", "email": "a@x.test"}),
        # Double-encoded (json.dumps of the single-encoded string): both layers unwrapped.
        (json.dumps('{"full_name": "Ada", "email": "a@x.test"}'), {"full_name": "Ada", "email": "a@x.test"}),
        (None, None),
        ("", None),
        ("   ", None),
        ("null", None),  # JSON null is genuinely no payload, not {"task_data": None}
        ("just a plain string", {"task_data": "just a plain string"}),
        (["a", "b"], {"task_data": ["a", "b"]}),
    ],
)
def test_coerce_v3_parameters_surfaces_payload_regardless_of_type(payload: object, expected: object) -> None:
    assert coerce_v3_parameters(payload) == expected


@pytest.mark.asyncio
async def test_page_free_mode_has_no_browser_tools_and_page_free_prompt() -> None:
    # Structural, not advisory: a page-free run exposes no perception/action tools, its system
    # prompt never instructs observing, and an attempted observe is an unknown tool.
    script = [[("observe", {})], [("finish", {"status": "completed", "reason": "criteria hold"})]]
    caller = _ScriptedCaller(script)

    async def no_page() -> Any:
        raise AssertionError("page provider must never be consulted in page-free mode")

    outcome = await run_task_v3_agent_loop(
        page_provider=no_page,
        llm_caller=caller,
        goal="assess",
        page_free=True,
        max_action_steps=2,
        max_turns=6,
    )
    assert outcome.status == "completed"
    tool_messages = [m["content"] for m in outcome.messages if m.get("role") == "tool"]
    assert any("unknown_tool: observe" in c for c in tool_messages)
    system_message = next(m for m in outcome.messages if m.get("role") == "system")
    assert "NO browser tools" in system_message["content"]
    assert "Perceive with" not in system_message["content"]


@pytest.mark.asyncio
async def test_engine_defers_completion_while_delayed_render_settles(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression for the fixture's delayed-states pattern: a panel's data loads AFTER a delay, and
    # a completion verdict issued mid-render must be deferred until two DOM samples match. The fake
    # page mutates its fingerprint once (the delayed load landing), like the fixture's
    # loading -> loaded transition.
    monkeypatch.setattr("skyvern.forge.taskv3.loop.asyncio.sleep", AsyncMock(return_value=None))
    samples = iter(["loading-shell", "loaded-panel", "loaded-panel", "loaded-panel"])

    async def page_fingerprint() -> str | None:
        return next(samples, "loaded-panel")

    async def provider() -> Any:
        return object()

    script = [
        [("finish", {"status": "completed", "reason": "panel visible"})],
        [("finish", {"status": "completed", "reason": "panel content confirmed"})],
    ]
    caller = _ScriptedCaller(script)
    outcome = await run_task_v3_agent_loop(
        page_provider=provider,
        llm_caller=caller,
        goal="open the panel",
        page_fingerprint=page_fingerprint,
        max_action_steps=2,
        max_turns=8,
    )
    assert outcome.status == "completed"
    assert outcome.reason == "panel content confirmed"


@pytest.mark.asyncio
async def test_page_free_mode_finishes_without_settle_probe() -> None:
    # Page-free runs have no page to settle: finish(completed) is immediate and the provider is
    # never consulted.
    async def no_page() -> Any:
        raise AssertionError("provider must not be consulted in page-free mode")

    script = [[("finish", {"status": "completed", "reason": "criteria hold"})]]
    caller = _ScriptedCaller(script)
    outcome = await run_task_v3_agent_loop(
        page_provider=no_page,
        llm_caller=caller,
        goal="assess",
        page_free=True,
        max_action_steps=2,
        max_turns=4,
    )
    assert outcome.status == "completed"


@pytest.mark.asyncio
async def test_bare_run_finishes_without_settle_probe() -> None:
    # Fenced: without a page_fingerprint sampler (the bare-task default) finish(completed) never
    # consults the page, preserving the live bare-task arm's finish path.
    sample_calls = 0

    async def counting_fingerprint() -> str | None:
        nonlocal sample_calls
        sample_calls += 1
        return "fp"

    async def provider() -> Any:
        return object()

    script = [[("finish", {"status": "completed", "reason": "done"})]]
    caller = _ScriptedCaller(script)
    outcome = await run_task_v3_agent_loop(
        page_provider=provider, llm_caller=caller, goal="g", max_action_steps=2, max_turns=4
    )
    assert outcome.status == "completed"
    assert sample_calls == 0


@pytest.mark.asyncio
async def test_engine_omits_tool_choice_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.taskv3 import engine as engine_mod
    from skyvern.forge.taskv3.loop import LoopOutcome

    captured: dict[str, object] = {}

    async def _capture(**kwargs: object) -> LoopOutcome:
        captured.update(kwargs)
        return LoopOutcome(status="completed", reason="ok")

    monkeypatch.setattr(engine_mod, "run_agent_tool_loop", _capture)
    monkeypatch.setattr(engine_mod.settings, "TASK_V3_TOOL_CHOICE_REQUIRED", False)

    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()), llm_caller=_ScriptedCaller([]), goal="x"
    )
    # None, not {} -- the loop splats **(call_kwargs or {}), so preserving None keeps the
    # default (lever-off) path byte-identical to before this lever existed.
    assert captured["call_kwargs"] is None

    step = object()
    captured.clear()
    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()), llm_caller=_ScriptedCaller([]), goal="x", step=step
    )
    assert captured["call_kwargs"] == {"step": step}


@pytest.mark.asyncio
async def test_engine_requests_tool_choice_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.taskv3 import engine as engine_mod
    from skyvern.forge.taskv3.loop import LoopOutcome

    captured: dict[str, object] = {}

    async def _capture(**kwargs: object) -> LoopOutcome:
        captured.update(kwargs)
        return LoopOutcome(status="completed", reason="ok")

    monkeypatch.setattr(engine_mod, "run_agent_tool_loop", _capture)
    monkeypatch.setattr(engine_mod.settings, "TASK_V3_TOOL_CHOICE_REQUIRED", True)

    step = object()
    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()), llm_caller=_ScriptedCaller([]), goal="x", step=step
    )

    assert captured["call_kwargs"] == {"step": step, "tool_choice": "required"}

    # The engine asking the caller is what keeps tool_choice_in_effect honest rather than
    # aspirational: a model that cannot take the parameter must not have it added at all.
    class _UnsupportedCaller(_ScriptedCaller):
        def supports_tool_choice(self) -> bool:
            return False

    captured.clear()
    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()), llm_caller=_UnsupportedCaller([]), goal="x", step=step
    )

    assert captured["call_kwargs"] == {"step": step}


@pytest.mark.asyncio
async def test_engine_wires_failure_evidence_gate() -> None:
    # End-to-end wiring: the engine's own ActivityRecency reaches both the loop (which records the
    # solve_captcha attempt) and the finish tool (which holds the failure verdict for one evidence
    # turn). Without either half the first finish(failed) would be accepted immediately.
    async def solve_captcha_handler(args: Any) -> ToolResult:
        return ToolResult.error("a captcha challenge is present but could not be solved this attempt")

    captcha_tool = ToolSpec(
        name="solve_captcha",
        description="solve_captcha",
        parameters={"type": "object", "properties": {}},
        handler=solve_captcha_handler,
        recordable=True,
    )

    async def page_fingerprint() -> str | None:
        return "fp"

    async def provider() -> Any:
        return object()

    script = [
        [("solve_captcha", {})],
        [("finish", {"status": "failed", "reason": "could_not_pass_captcha"})],
        [("finish", {"status": "failed", "reason": "still blocked, re-verified"})],
    ]
    caller = _ScriptedCaller(script)
    outcome = await run_task_v3_agent_loop(
        page_provider=provider,
        llm_caller=caller,
        goal="apply",
        page_fingerprint=page_fingerprint,
        extra_tools=[captcha_tool],
        max_action_steps=4,
        max_turns=8,
    )
    assert outcome.status == "failed"
    assert outcome.reason == "still blocked, re-verified"


@pytest.mark.asyncio
async def test_engine_wires_the_pending_gate_and_withholds_it_from_page_free_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The gate needs BOTH halves to reach their destinations and to share one record: the loop writes
    # the clicked control into the watch, the finish tool reads it. Wire either half to a different
    # object, or arm them for a page-free run (which has no page to ask) and disarm them for an
    # ordinary one, and nothing else in the suite would notice.
    from skyvern.forge.taskv3 import engine as engine_mod
    from skyvern.forge.taskv3.loop import LoopOutcome, SubmitWatch

    finish_args: list[tuple[Any, Any]] = []
    loop_watches: list[Any] = []
    real_make = engine_mod.make_finish_tool
    real_loop = engine_mod.run_agent_tool_loop

    def capturing_make(*args: Any, **kwargs: Any) -> Any:
        finish_args.append((kwargs.get("pending_marker"), kwargs.get("submit_watch")))
        return real_make(*args, **kwargs)

    async def capturing_loop(**kwargs: Any) -> LoopOutcome:
        loop_watches.append(kwargs.get("submit_watch"))
        return await real_loop(**kwargs)

    monkeypatch.setattr(engine_mod, "make_finish_tool", capturing_make)
    monkeypatch.setattr(engine_mod, "run_agent_tool_loop", capturing_loop)

    async def provider() -> Any:
        return object()

    async def pending_marker(selector: str) -> str | None:
        return "the submit control still reads 'Submitting…'"

    script = [[("finish", {"status": "completed", "reason": "done"})]]
    await run_task_v3_agent_loop(
        page_provider=provider,
        llm_caller=_ScriptedCaller(script),
        goal="apply",
        pending_marker=pending_marker,
        max_action_steps=2,
        max_turns=4,
    )
    await run_task_v3_agent_loop(
        page_provider=provider,
        llm_caller=_ScriptedCaller([[("finish", {"status": "completed", "reason": "criteria hold"})]]),
        goal="assess",
        page_free=True,
        pending_marker=pending_marker,
        max_action_steps=2,
        max_turns=4,
    )
    assert [marker for marker, _watch in finish_args] == [pending_marker, None], finish_args
    watches = [watch for _marker, watch in finish_args]
    assert isinstance(watches[0], SubmitWatch), watches
    assert watches[1] is None, watches
    assert loop_watches[0] is watches[0], (loop_watches, watches)
    assert loop_watches[1] is None, loop_watches


@pytest.mark.asyncio
async def test_signed_payload_url_reaches_model_only_as_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # A presigned file URL reaches the model only as its opaque_url_ token in the payload; the tools
    # resolve that token to the untouched bytes (navigate's goto, file_upload's download), and the
    # finish output is un-masked so the customer never sees the token.
    segment = ("0123456789abcdef" * 3)[:40]
    host_and_path = f"https://files.example.test/uploads/{segment}/resume.pdf"
    credential_value = "AKIAEXAMPLE0123456%2F20260824%2Fus-east-1%2Fs3%2Faws4_request"
    signature = "f1e2d3c4b5a697887766554433221100aabbccddeeff00112233445566778899"
    signed_url = (
        f"{host_and_path}?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential={credential_value}"
        f"&X-Amz-Date=20260824T000000Z&X-Amz-Expires=3600&X-Amz-SignedHeaders=host&X-Amz-Signature={signature}"
    )
    plain_url = "https://portfolio.example.test/jo"
    parameters = {"first_name": "Jo", "resume_url": signed_url, "portfolio_url": plain_url}
    token = next(iter(mask_opaque_urls(parameters).refs))

    import skyvern.utils.url_validators as urlv

    monkeypatch.setattr(urlv, "validate_fetch_url", lambda url: url)

    captured_source: dict[str, str] = {}

    async def fake_download_file(source: str, output_dir: str | None = None, organization_id: str | None = None) -> str:
        captured_source["source"] = source
        request_info = aiohttp.RequestInfo(
            url=yarl.URL(signed_url), method="GET", headers={}, real_url=yarl.URL(signed_url)
        )
        raise aiohttp.ClientResponseError(request_info=request_info, history=(), status=400, message="Bad Request")

    import skyvern.forge.sdk.api.files as files_module

    monkeypatch.setattr(files_module, "download_file", fake_download_file)

    script = [
        [("navigate", {"url": token})],
        [("file_upload", {"selector": "#cv", "file": token})],
        [
            (
                "finish",
                {
                    "status": "failed",
                    "reason": f"upload of {token} was rejected",
                    "extracted_output": {"uploaded": [token], "note": f"used {token}"},
                },
            )
        ],
    ]
    caller = _ScriptedCaller(script)
    page = _FakePage()
    ctx = SkyvernContext(task_id="tsk_1")
    skyvern_context.set(ctx)
    try:
        outcome = await run_task_v3_agent_loop(
            page_provider=_fixed_page_provider(page),
            llm_caller=caller,
            goal="Apply to the role and upload the attached resume.",
            parameters=parameters,
        )
        assert ctx.runtime_secret_values == set()  # the customer's own URL is never enrolled in redaction
    finally:
        skyvern_context.reset()

    assert captured_source["source"] == signed_url  # the real bytes reached the download call
    assert page.url == signed_url  # navigate resolved the token before goto

    user_prompt = next(m["content"] for m in outcome.messages if m.get("role") == "user")
    assert signed_url not in user_prompt
    assert signature not in user_prompt and credential_value not in user_prompt
    assert token in user_prompt
    assert plain_url in user_prompt  # nosemgrep: incomplete-url-substring-sanitization

    assert outcome.status == "failed"
    assert signed_url in outcome.reason  # nosemgrep: incomplete-url-substring-sanitization
    assert token not in outcome.reason
    assert outcome.extracted_output == {"uploaded": [signed_url], "note": f"used {signed_url}"}


@pytest.mark.asyncio
async def test_business_identifier_value_under_a_non_signing_key_stays_readable() -> None:
    # A token-shaped VALUE under an ordinary business KEY (order id, not a signing param) must never be
    # tokenized: neither in the payload nor in ordinary page content the model reads.
    order_id = "ORD2026AUG24X7Q1A"
    order_url = f"https://shop.example.test/orders?orderId={order_id}"
    parameters = {"order_url": order_url}

    class _OrderPage(_FakePage):
        async def content(self) -> str:
            return f"<html><body>Order {order_id} shipped</body></html>"

    script = [
        [("get_html", {})],
        [("finish", {"status": "completed", "reason": "done"})],
    ]
    caller = _ScriptedCaller(script)
    outcome = await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_OrderPage()),
        llm_caller=caller,
        goal="check order status",
        parameters=parameters,
    )
    user_message = next(m for m in outcome.messages if m.get("role") == "user")["content"]
    assert order_url in user_message  # nosemgrep: incomplete-url-substring-sanitization
    assert "opaque_url_" not in user_message

    tool_messages = {m["name"]: m["content"] for m in outcome.messages if m.get("role") == "tool"}
    assert f"Order {order_id} shipped" in tool_messages["get_html"]


@pytest.mark.asyncio
async def test_hash_route_job_url_is_not_masked() -> None:
    # A SPA hash-route job URL is an ordinary payload value, not a signed URL: it must reach the
    # model verbatim in the user prompt, not as an opaque_url_ token.
    job_url = "https://careers.example.test/#/jobs/software-engineer-2026"
    parameters = {"job_url": job_url}
    caller = _ScriptedCaller([[("finish", {"status": "completed", "reason": "done"})]])
    ctx = SkyvernContext(task_id="tsk_2")
    skyvern_context.set(ctx)
    try:
        outcome = await run_task_v3_agent_loop(
            page_provider=_fixed_page_provider(_FakePage()),
            llm_caller=caller,
            goal="Apply using the given job URL.",
            parameters=parameters,
        )
    finally:
        skyvern_context.reset()

    transcript = json.dumps(outcome.messages)
    assert job_url in transcript  # nosemgrep: incomplete-url-substring-sanitization
    assert "opaque_url_" not in transcript


@pytest.mark.asyncio
async def test_page_free_mode_does_not_mask_signed_urls() -> None:
    # Page-free mode has no tools to resolve an opaque_url_ token, so the payload stays verbatim.
    signed_url = "https://files.example.test/uploads/x?token=eyJhbGciOiJIUzI1NiJ9c2lnbmVkQ29ycmVjdEhvcnNl"
    caller = _ScriptedCaller([[("finish", {"status": "completed", "reason": "criteria hold"})]])
    outcome = await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()),  # never consulted: page_free has no tools
        llm_caller=caller,
        goal="assess",
        page_free=True,
        parameters={"u": signed_url},
        max_turns=4,
    )
    user_message = next(m for m in outcome.messages if m.get("role") == "user")["content"]
    assert (
        signed_url in user_message and "opaque_url_" not in user_message
    )  # nosemgrep: incomplete-url-substring-sanitization


@pytest.mark.asyncio
async def test_engine_forwards_completion_hooks_and_gates_guidance_on_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    # completion_probe must reach the loop and completion_blocker must reach the finish tool, and
    # the download-completion guidance is appended to the system prompt only when a probe is given.
    from skyvern.forge.taskv3 import engine as engine_mod
    from skyvern.forge.taskv3.engine import DOWNLOAD_COMPLETION_GUIDANCE
    from skyvern.forge.taskv3.loop import LoopOutcome

    loop_kwargs: dict[str, Any] = {}
    finish_kwargs: dict[str, Any] = {}

    async def fake_loop(**kwargs: Any) -> LoopOutcome:
        loop_kwargs.update(kwargs)
        return LoopOutcome(status="completed", reason="ok")

    real_make_finish_tool = engine_mod.make_finish_tool

    def capturing_make_finish_tool(*args: Any, **kwargs: Any) -> Any:
        finish_kwargs.update(kwargs)
        return real_make_finish_tool(*args, **kwargs)

    monkeypatch.setattr(engine_mod, "run_agent_tool_loop", fake_loop)
    monkeypatch.setattr(engine_mod, "make_finish_tool", capturing_make_finish_tool)

    async def probe(_staged: frozenset[str]) -> str | None:
        return "a file finished downloading"

    async def blocker(_staged: frozenset[str]) -> str | None:
        return None

    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()),
        llm_caller=_ScriptedCaller([]),
        goal="download the file",
        completion_probe=probe,
        completion_blocker=blocker,
    )
    assert loop_kwargs["completion_probe"] is probe
    assert finish_kwargs["completion_blocker"] is blocker
    assert DOWNLOAD_COMPLETION_GUIDANCE in loop_kwargs["system_prompt"]
    # One staged_downloads set is shared between the loop and the finish tool -- a name staged via
    # a billable tool call must be visible to the SAME finish-tool blocker, not a divergent copy.
    assert loop_kwargs["staged_downloads"] is finish_kwargs["staged_downloads"]
    assert isinstance(loop_kwargs["staged_downloads"], set)

    loop_kwargs.clear()
    finish_kwargs.clear()
    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()), llm_caller=_ScriptedCaller([]), goal="download the file"
    )
    assert loop_kwargs["completion_probe"] is None
    assert finish_kwargs["completion_blocker"] is None
    assert DOWNLOAD_COMPLETION_GUIDANCE not in loop_kwargs["system_prompt"]


@pytest.mark.asyncio
async def test_engine_guidance_keyed_on_which_hooks_are_present(monkeypatch: pytest.MonkeyPatch) -> None:
    # blocker-only (extraction tasks: the probe would end the loop before the model returns
    # extracted_output) gets the DOWNLOAD_REQUIRED variant naming finish(completed) as the model's
    # own job; probe-only (download_timeout alone, wait-only) has no completion semantics at all,
    # so it gets neither guidance string.
    from skyvern.forge.taskv3 import engine as engine_mod
    from skyvern.forge.taskv3.engine import DOWNLOAD_COMPLETION_GUIDANCE, DOWNLOAD_REQUIRED_GUIDANCE
    from skyvern.forge.taskv3.loop import LoopOutcome

    loop_kwargs: dict[str, Any] = {}

    async def fake_loop(**kwargs: Any) -> LoopOutcome:
        loop_kwargs.update(kwargs)
        return LoopOutcome(status="completed", reason="ok")

    monkeypatch.setattr(engine_mod, "run_agent_tool_loop", fake_loop)

    async def blocker(_staged: frozenset[str]) -> str | None:
        return None

    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()),
        llm_caller=_ScriptedCaller([]),
        goal="download the file",
        completion_blocker=blocker,
    )
    assert DOWNLOAD_REQUIRED_GUIDANCE in loop_kwargs["system_prompt"]
    assert DOWNLOAD_COMPLETION_GUIDANCE not in loop_kwargs["system_prompt"]

    loop_kwargs.clear()

    async def probe(_staged: frozenset[str]) -> str | None:
        return None

    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()),
        llm_caller=_ScriptedCaller([]),
        goal="download the file",
        completion_probe=probe,
    )
    assert DOWNLOAD_REQUIRED_GUIDANCE not in loop_kwargs["system_prompt"]
    assert DOWNLOAD_COMPLETION_GUIDANCE not in loop_kwargs["system_prompt"]


@pytest.mark.asyncio
async def test_engine_drops_download_hooks_in_page_free_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    # No browser tools exist to trigger a download, so a blocker would refuse finish(completed)
    # forever; page-free runs get neither hook nor the download guidance.
    from skyvern.forge.taskv3 import engine as engine_mod
    from skyvern.forge.taskv3.engine import DOWNLOAD_COMPLETION_GUIDANCE, DOWNLOAD_REQUIRED_GUIDANCE
    from skyvern.forge.taskv3.loop import LoopOutcome

    loop_kwargs: dict[str, Any] = {}
    finish_kwargs: dict[str, Any] = {}

    async def fake_loop(**kwargs: Any) -> LoopOutcome:
        loop_kwargs.update(kwargs)
        return LoopOutcome(status="completed", reason="ok")

    real_make_finish_tool = engine_mod.make_finish_tool

    def capturing_make_finish_tool(*args: Any, **kwargs: Any) -> Any:
        finish_kwargs.update(kwargs)
        return real_make_finish_tool(*args, **kwargs)

    monkeypatch.setattr(engine_mod, "run_agent_tool_loop", fake_loop)
    monkeypatch.setattr(engine_mod, "make_finish_tool", capturing_make_finish_tool)

    async def probe(_staged: frozenset[str]) -> str | None:
        return "a file finished downloading"

    async def blocker(_staged: frozenset[str]) -> str | None:
        return "no download yet"

    async def verification_blocker() -> str | None:
        return "no code arrived"

    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()),
        llm_caller=_ScriptedCaller([]),
        goal="validate the record",
        page_free=True,
        completion_probe=probe,
        completion_blocker=blocker,
        verification_blocker=verification_blocker,
    )
    assert loop_kwargs["completion_probe"] is None
    assert finish_kwargs["completion_blocker"] is None
    assert finish_kwargs["verification_blocker"] is None
    assert DOWNLOAD_COMPLETION_GUIDANCE not in loop_kwargs["system_prompt"]
    assert DOWNLOAD_REQUIRED_GUIDANCE not in loop_kwargs["system_prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("page_free", [False, True])
async def test_engine_system_prompt_carries_the_current_date(monkeypatch: pytest.MonkeyPatch, page_free: bool) -> None:
    # A relative-date goal ("two weeks from today") is unanswerable without a reference date; the
    # model was observed typing past dates. The date is computed at assembly, never hardcoded.
    from datetime import UTC, datetime

    from skyvern.forge.taskv3 import engine as engine_mod
    from skyvern.forge.taskv3.loop import LoopOutcome

    loop_kwargs: dict[str, Any] = {}

    async def fake_loop(**kwargs: Any) -> LoopOutcome:
        loop_kwargs.update(kwargs)
        return LoopOutcome(status="completed", reason="ok")

    monkeypatch.setattr(engine_mod, "run_agent_tool_loop", fake_loop)
    before = datetime.now(UTC)
    await run_task_v3_agent_loop(
        page_provider=_fixed_page_provider(_FakePage()),
        llm_caller=_ScriptedCaller([]),
        goal="fill in the date available: two weeks from today",
        page_free=page_free,
    )
    after = datetime.now(UTC)
    system_prompt = loop_kwargs["system_prompt"]
    assert any(
        f"Today's date is {d.strftime('%Y-%m-%d')} ({d.strftime('%A')})" in system_prompt for d in (before, after)
    ), system_prompt[-300:]


@pytest.mark.asyncio
async def test_engine_system_prompt_dates_in_the_runs_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    # The browser runs in the proxy's timezone (browser_factory sets ctx.tz_info); the stated date
    # must be that zone's today, or UTC drifts a day ahead of/behind the page every evening.
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from skyvern.forge.taskv3 import engine as engine_mod
    from skyvern.forge.taskv3.loop import LoopOutcome

    loop_kwargs: dict[str, Any] = {}

    async def fake_loop(**kwargs: Any) -> LoopOutcome:
        loop_kwargs.update(kwargs)
        return LoopOutcome(status="completed", reason="ok")

    monkeypatch.setattr(engine_mod, "run_agent_tool_loop", fake_loop)
    # One of the two extreme zones is always on a different calendar day than UTC.
    tz = next(
        z
        for z in (ZoneInfo("Pacific/Kiritimati"), ZoneInfo("Etc/GMT+12"))
        if datetime.now(z).date() != datetime.now(UTC).date()
    )
    ctx = SkyvernContext(task_id="tsk_tz")
    ctx.tz_info = tz
    skyvern_context.set(ctx)
    try:
        await run_task_v3_agent_loop(
            page_provider=_fixed_page_provider(_FakePage()),
            llm_caller=_ScriptedCaller([]),
            goal="fill in the date available: two weeks from today",
        )
    finally:
        skyvern_context.reset()
    system_prompt = loop_kwargs["system_prompt"]
    assert f"Today's date is {datetime.now(tz).strftime('%Y-%m-%d')}" in system_prompt, system_prompt[-200:]
    assert f"Today's date is {datetime.now(UTC).strftime('%Y-%m-%d')}" not in system_prompt
