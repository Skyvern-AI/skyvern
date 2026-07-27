"""Observation epochs and batch/action preflight wiring (SKY-12874).

Every test here is written to fail when the thing it guards is *removed*, not merely to pass while
it is present. Where that means asserting an ordering, the spy raises so the test can prove nothing
downstream ran; where it means asserting a deny, the request is arranged so the deny can only come
from the gate under test.
"""

from __future__ import annotations

import ast
import gc
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.config import settings
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk import browser_action_preflight as preflight
from skyvern.forge.sdk.browser_action_policy import (
    ActionProjection,
    ActionTarget,
    AuthorityState,
    BrowserActionRequest,
    PolicyOutcome,
    PolicyReason,
    RuntimeOriginAuthority,
    declare_origin,
    declare_policy,
)
from skyvern.forge.sdk.browser_action_preflight import (
    advance_observation_epoch,
    observed_page,
    preflight_action,
    preflight_batch,
    preflight_derived_action,
    stamp_parsed_actions,
)
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.webeye.actions import actions as action_models
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.handler import ActionHandler

HOME = "https://example.com/start"
OWNER = "wr_12874"
POLICY = declare_policy(owner_id=OWNER, origin_urls=[HOME])
ESTABLISHED = RuntimeOriginAuthority(state=AuthorityState.ESTABLISHED, origins=POLICY.allowed_origins)

# Every action model that takes no required argument, so the surviving set can be measured rather
# than asserted from memory.
CONCRETE_ACTIONS_FOR_SURVIVAL = (
    action_models.WaitAction,
    action_models.ScrollAction,
    action_models.MoveAction,
    action_models.NullAction,
    action_models.GoBackAction,
    action_models.GoForwardAction,
    action_models.ClosePageAction,
    action_models.ReloadPageAction,
    action_models.TerminateAction,
    action_models.CompleteAction,
    action_models.ExtractAction,
    action_models.SolveCaptchaAction,
    lambda: action_models.HoverAction(element_id="1"),
    lambda: action_models.ClickAction(element_id="1"),
    lambda: action_models.SwitchTabAction(tab_index=0),
    lambda: action_models.GotoUrlAction(url=HOME),
)


class FakePage:
    """Just enough Page for the preflight: a URL, a main frame and an event registry.

    The registry is real rather than a MagicMock so listener leaks are observable — a mock would
    happily record a thousand registrations and assert nothing.
    """

    def __init__(self, url: str = HOME) -> None:
        self.url = url
        self.main_frame = MagicMock()
        self.main_frame.url = url
        self.listeners: dict[str, list] = {}
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        """Playwright keeps serving the cached url and main_frame.url after close; it does not
        raise. A fake that raised here would be more hostile than reality in the one way that
        hides a closed page reading as observable."""
        self.closed = True

    def on(self, event: str, handler) -> None:
        self.listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler) -> None:
        self.listeners.get(event, []).remove(handler)

    def navigate_to(self, url: str) -> None:
        """A cross-document navigation, which IS observable: the main-frame URL changes."""
        self.url = url
        self.main_frame.url = url


class ExplodingPage(FakePage):
    """A page whose `url` raises. Not what closing looks like — see FakePage.close — but a stand-in
    for any unexpected fault inside the preflight, which must never escape."""

    def __getattribute__(self, name: str):
        if name == "url":
            raise RuntimeError("boom")
        return object.__getattribute__(self, name)


@pytest.fixture(autouse=True)
def observing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "BROWSER_ACTION_POLICY_MODE", "observe")
    context = SkyvernContext(request_id="req_12874", browser_action_policy=POLICY)
    skyvern_context.set(context)
    try:
        yield context
    finally:
        skyvern_context.reset()


def scrape(page: FakePage, context: SkyvernContext, *, hashes: tuple[str, ...] = ("h1", "h2")) -> None:
    advance_observation_epoch(page, main_frame_url=page.main_frame.url, element_hashes=hashes)


class TestObservationEpochs:
    def test_each_accepted_scrape_advances_the_epoch(self, observing: SkyvernContext) -> None:
        page = FakePage()
        assert observing.browser_observation_epoch is None
        scrape(page, observing)
        assert observing.browser_observation_epoch is not None
        assert observing.browser_observation_epoch.epoch == 1
        scrape(page, observing)
        assert observing.browser_observation_epoch.epoch == 2

    def test_the_epoch_is_bound_to_the_scraped_element_hashes(self, observing: SkyvernContext) -> None:
        page = FakePage()
        scrape(page, observing, hashes=("h1", "h2"))
        first = observing.browser_observation_epoch
        scrape(page, observing, hashes=("h1", "h3"))
        second = observing.browser_observation_epoch
        assert first is not None and second is not None
        assert first.element_digest != second.element_digest

    def test_hash_order_does_not_change_the_binding(self, observing: SkyvernContext) -> None:
        page = FakePage()
        scrape(page, observing, hashes=("h1", "h2"))
        first = observing.browser_observation_epoch
        scrape(page, observing, hashes=("h2", "h1"))
        second = observing.browser_observation_epoch
        assert first is not None and second is not None
        assert first.element_digest == second.element_digest

    def test_the_epoch_is_bound_to_the_page_it_observed(self, observing: SkyvernContext) -> None:
        observed, other = FakePage(), FakePage()
        scrape(observed, observing)
        action = action_models.ClickAction(element_id="1")
        stamp_parsed_actions([action])
        decision = preflight_action(action, other, site="test")
        assert decision is not None
        # Another tab's observation vouches for nothing on this one.
        assert PolicyReason.MISSING_PAGE_EVIDENCE in decision.reasons

    def test_navigation_since_the_scrape_invalidates_the_observation(self, observing: SkyvernContext) -> None:
        page = FakePage()
        scrape(page, observing)
        action = action_models.ClickAction(element_id="1")
        stamp_parsed_actions([action])
        page.main_frame.url = "https://example.com/somewhere-else"
        decision = preflight_action(action, page, site="test")
        assert decision is not None
        assert PolicyReason.MISSING_PAGE_EVIDENCE in decision.reasons

    def test_an_action_planned_under_an_older_scrape_is_stale(self, observing: SkyvernContext) -> None:
        page = FakePage()
        scrape(page, observing)
        action = action_models.ClickAction(element_id="1")
        stamp_parsed_actions([action])
        scrape(page, observing)
        decision = preflight_action(action, page, site="test")
        assert decision is not None
        assert PolicyReason.STALE_PAGE_EVIDENCE in decision.reasons

    def test_an_unstamped_action_carries_no_observation(self, observing: SkyvernContext) -> None:
        page = FakePage()
        scrape(page, observing)
        decision = preflight_action(action_models.ClickAction(element_id="1"), page, site="test")
        assert decision is not None
        assert PolicyReason.MISSING_PAGE_EVIDENCE in decision.reasons

    def test_a_closed_page_is_not_observable(self, observing: SkyvernContext) -> None:
        # Playwright serves url and main_frame.url from cache after close, so every other check
        # here still passes on a page that no longer exists.
        page = FakePage()
        scrape(page, observing)
        action = action_models.ClickAction(element_id="1")
        stamp_parsed_actions([action])
        assert PolicyReason.MISSING_PAGE_EVIDENCE not in preflight_action(action, page, site="test").reasons

        page.close()

        decision = preflight_action(action, page, site="test")
        assert decision is not None
        assert PolicyReason.MISSING_PAGE_EVIDENCE in decision.reasons

    def test_a_closed_page_still_reports_its_cached_url(self) -> None:
        # Pins the fake to the real object's behaviour. If this ever starts raising, the fake has
        # drifted more hostile than Playwright and will hide the bug above all over again.
        page = FakePage()
        page.close()
        assert page.url == HOME
        assert page.main_frame.url == HOME
        assert page.is_closed() is True

    def test_nothing_is_observed_while_the_mode_is_disabled(
        self, observing: SkyvernContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "BROWSER_ACTION_POLICY_MODE", "disabled")
        page = FakePage()
        scrape(page, observing)
        assert observing.browser_observation_epoch is None
        assert preflight_action(action_models.ClickAction(element_id="1"), page, site="test") is None


class TestTheScrapeAdvancesTheEpoch:
    """AC1. Every test above calls ``advance_observation_epoch`` directly, so on their own they
    prove the epoch machinery works and nothing about whether a scrape ever reaches it."""

    def test_an_accepted_scrape_is_what_advances_it(self) -> None:
        source = ast.parse(Path("skyvern/webeye/scraper/scraper.py").read_text())
        advancing = {
            node.name
            for node in ast.walk(source)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "advance_observation_epoch"
                for call in ast.walk(node)
            )
        }
        # Named, not merely non-empty: the retry wrapper `scrape_website` recurses, so advancing
        # there would count one accepted scrape twice.
        assert advancing == {"scrape_web_unsafe"}

    def test_it_advances_once_per_accepted_scrape(self, observing: SkyvernContext) -> None:
        # The wrapper returns the inner result rather than re-deriving one, so a retried scrape must
        # still land on a single epoch.
        page = FakePage()
        scrape(page, observing)
        assert observing.browser_observation_epoch is not None
        assert observing.browser_observation_epoch.epoch == 1


class TestStampingHappensAtTheParsePoint:
    """F2. prepare_step_execution can build an action BEFORE the step scrape runs. Stamping the
    whole batch later handed such an action provenance for an observation it predates."""

    @staticmethod
    async def _generate(injected: list | None, parsed, *, extraction: bool = True, otp=None) -> list:
        async def fake_extract(*_a, **_k):
            return parsed

        async def no_otp(*_a, **_k):
            return {}, []

        agent = MagicMock()
        agent.create_extract_action = fake_extract
        agent.handle_potential_OTP_actions = otp or no_otp
        scraped = MagicMock()
        scraped.check_pdf_viewer_embed.return_value = None
        scraped.check_pdf_iframe = AsyncMock(return_value=None)
        task = MagicMock()
        task.navigation_payload = {}
        task.navigation_goal = "goal"
        task.llm_key = None
        task.totp_verification_url = None
        task.totp_identifier = None
        task.organization_id = "o"
        task.workflow_run_id = "wr"
        task.task_id = "tsk"
        llm = AsyncMock(return_value={"actions": []})
        with (
            patch.object(ForgeAgent, "create_extract_action", fake_extract),
            patch("skyvern.forge.agent.LLMAPIHandlerFactory.get_override_llm_api_handler", return_value=llm),
        ):
            actions, _, _ = await ForgeAgent._generate_step_actions(
                agent,
                task=task,
                step=MagicMock(),
                browser_state=MagicMock(),
                engine=MagicMock(),
                scraped_page=scraped,
                detailed_agent_step_output=MagicMock(),
                injected_actions=injected,
                is_extraction_task=extraction,
                prefetched_summary_task=None,
                cua_response=None,
                llm_caller=None,
                extract_action_prompt="",
                prompt_name="",
                use_caching=False,
                without_page_information=False,
                json_response=None,
                reuse_speculative_llm_response=False,
                speculative_llm_metadata=None,
                context=None,
            )
        return actions

    @pytest.mark.asyncio
    async def test_an_action_injected_before_the_scrape_is_never_stamped(self, observing: SkyvernContext) -> None:
        scrape(FakePage(), observing)
        injected = action_models.SolveCaptchaAction()
        actions = await self._generate([injected], None)
        assert actions == [injected]
        assert injected.observation_epoch is None
        assert injected.observation_digest is None

    @pytest.mark.asyncio
    async def test_a_runtime_derived_otp_action_is_never_stamped(self, observing: SkyvernContext) -> None:
        # The magic-link path: polling supplies a link, browser_state.new_page() opens a page nobody
        # has scraped, and the code synthesizes a GotoUrlAction — all with injected_actions None, so
        # a blanket end-of-method stamp reached it. Provenance for a page that was never observed.
        scrape(FakePage(), observing)
        otp_action = action_models.GotoUrlAction(url="https://mail.example/magic-link")

        async def fake_otp(*_a, **_k):
            return {}, [otp_action]

        actions = await self._generate(None, None, extraction=False, otp=fake_otp)
        assert actions == [otp_action]
        assert otp_action.observation_epoch is None
        assert otp_action.observation_digest is None

    @pytest.mark.asyncio
    async def test_a_scraped_verification_code_action_is_stamped(self, observing: SkyvernContext) -> None:
        # The mirror image of the OTP test above, and it shares that return path. When the model
        # asks for a verification code, handle_potential_verification_code re-prompts from the
        # CURRENT scraped page and parse_actions builds from it — genuinely scrape-derived. Leaving
        # it unstamped is fail-closed but wrong: observe telemetry lies, and under enforcement a
        # valid TOTP step would be blocked.
        scrape(FakePage(), observing)
        epoch = observing.browser_observation_epoch
        assert epoch is not None
        parsed = action_models.InputTextAction(element_id="1", text="123456")

        agent = MagicMock()
        agent.handle_potential_verification_code = AsyncMock(return_value={"actions": []})
        task = MagicMock()
        task.organization_id = "o"
        task.totp_identifier = "id"
        task.totp_verification_url = None

        with patch("skyvern.forge.agent.parse_actions", return_value=[parsed]):
            _, actions = await ForgeAgent.handle_potential_OTP_actions(
                agent,
                task,
                MagicMock(),
                MagicMock(),
                MagicMock(),
                {"place_to_enter_verification_code": True, "should_enter_verification_code": True},
            )

        assert actions == [parsed]
        assert parsed.observation_epoch == epoch.epoch
        assert parsed.observation_digest == epoch.element_digest

    @pytest.mark.asyncio
    async def test_a_magic_link_action_from_the_same_return_path_stays_unstamped(
        self, observing: SkyvernContext
    ) -> None:
        # The twin that makes the test above mean something: same method, same tuple shape, opposite
        # outcome. A fix that stamped the shared return would pass the test above and fail this one.
        scrape(FakePage(), observing)
        magic_link = action_models.GotoUrlAction(url="https://mail.example/magic-link")

        agent = MagicMock()
        agent.handle_potential_magic_link = AsyncMock(return_value=[magic_link])
        task = MagicMock()
        task.organization_id = "o"
        task.totp_identifier = "id"
        task.totp_verification_url = None

        _, actions = await ForgeAgent.handle_potential_OTP_actions(
            agent, task, MagicMock(), MagicMock(), MagicMock(), {"should_verify_by_magic_link": True}
        )

        assert actions == [magic_link]
        assert magic_link.observation_epoch is None
        assert magic_link.observation_digest is None

    @pytest.mark.asyncio
    async def test_an_action_parsed_from_the_scrape_is_stamped(self, observing: SkyvernContext) -> None:
        # The twin. Without it every test above passes against an implementation that never stamps.
        scrape(FakePage(), observing)
        epoch = observing.browser_observation_epoch
        assert epoch is not None
        parsed = action_models.ExtractAction()
        actions = await self._generate(None, parsed)
        assert actions == [parsed]
        assert parsed.observation_epoch == epoch.epoch
        assert parsed.observation_digest == epoch.element_digest

    @pytest.mark.asyncio
    async def test_the_llm_parse_branch_is_stamped(self, observing: SkyvernContext) -> None:
        # The other twin, on the branch the OTP test shares: same code path, opposite outcome.
        scrape(FakePage(), observing)
        epoch = observing.browser_observation_epoch
        assert epoch is not None
        parsed = action_models.ClickAction(element_id="1")
        with patch("skyvern.forge.agent.parse_actions", return_value=[parsed]):
            actions = await self._generate(None, None, extraction=False)
        assert actions == [parsed]
        assert parsed.observation_epoch == epoch.epoch
        assert parsed.observation_digest == epoch.element_digest


class TestObserveModeIsAPureObserver:
    """F1. must_get_working_page is not a getter — it re-pins the working page and, via
    list_valid_pages, closes over-limit tabs. Observe mode reaching it made observe diverge from
    disabled. These test the invariant, not that one call."""

    STATE_CHANGING = {
        "must_get_working_page",
        "get_working_page",
        "set_working_page",
        "set_active_page",
        "list_valid_pages",
        "reload_page",
        "navigate_to_url",
        "close",
        "goto",
        "bring_to_front",
        "evaluate",
    }

    def test_the_preflight_module_calls_nothing_state_changing(self) -> None:
        tree = ast.parse(Path("skyvern/forge/sdk/browser_action_preflight.py").read_text())
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert called, "found no calls at all — the scan is broken, not the module"
        assert not called & self.STATE_CHANGING

    def test_no_preflight_call_site_hands_over_browser_state(self) -> None:
        # The reviewer found F1 by behaviour rather than by diff, so this sweeps every site rather
        # than the one that was wrong.
        offenders = []
        for path in ("skyvern/forge/agent.py", "skyvern/webeye/actions/handler.py"):
            tree = ast.parse(Path(path).read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                if not node.func.id.startswith("preflight_"):
                    continue
                rendered = " ".join(ast.unparse(arg) for arg in node.args)
                if "browser_state" in rendered or "working_page" in rendered:
                    offenders.append((path, ast.unparse(node)))
        assert offenders == []

    def test_the_preflight_only_reads_the_page(self, observing: SkyvernContext) -> None:
        reads: list[str] = []

        internals = {"listeners", "on", "remove_listener", "navigate_to", "closed", "close"}

        class RecordingPage(FakePage):
            def __getattribute__(self, name: str):
                if not name.startswith("_") and name not in internals:
                    reads.append(name)
                return object.__getattribute__(self, name)

        page = RecordingPage()
        scrape(page, observing)
        action = action_models.ClickAction(element_id="1")
        stamp_parsed_actions([action])
        reads.clear()
        preflight_action(action, page, site="test")
        # url, main_frame and is_closed are read-only queries — observation. Anything else is the
        # page being steered. This list is the whole permission set, so an addition shows up here
        # and has to be justified rather than slipping in.
        assert set(reads) <= {"url", "main_frame", "is_closed"}


class TestContentFreshnessIsNotClaimed:
    """A page can replace its own content with document.write or innerHTML while the page object,
    the URL and the epoch all stay fixed, and Chromium emits NO navigation event for either. An
    earlier revision watched framenavigated here; a real browser never fires it for these cases, so
    it advertised coverage it did not have — and the unit test that "proved" it worked was firing
    the event itself, which tested the handler rather than the signal's existence.

    Nothing below simulates any signal. The page is mutated exactly as a real page mutates itself:
    invisibly. These tests pin what actually happens as a result.
    """

    @staticmethod
    def replace_document(page: FakePage) -> None:
        """What document.write or innerHTML= looks like from outside: nothing observable changes."""

    def test_a_same_document_replacement_is_not_detectable(self, observing: SkyvernContext) -> None:
        # A documentation lock rather than a guard: it fails if someone adds detection, forcing the
        # claims in _live_observation and the PR to be updated with it.
        page = FakePage()
        scrape(page, observing)
        action = action_models.ClickAction(element_id="1")
        stamp_parsed_actions([action])
        before = preflight_action(action, page, site="test")

        self.replace_document(page)

        after = preflight_action(action, page, site="test")
        assert before is not None and after is not None
        # Stated, not hidden: provenance cannot see this, and a test claiming otherwise would be
        # simulating a signal the browser does not emit.
        assert after.reasons == before.reasons

    def test_a_content_dependent_action_denies_anyway(self, observing: SkyvernContext) -> None:
        """The guarantee that survives: content trust rides the verdict axis, not the epoch, so an
        action whose safety depends on what the page says is denied whether or not the page was
        swapped underneath it.

        This is also a tripwire — see test_no_verdict_source_exists_yet for the half that binds it
        to production. On its own this assertion would go stale silently: the helper advances its
        own epoch and supplies no scanner result, so if production later took a verdict and passed
        NO_MATCH, the fake would keep supplying UNKNOWN and this would stay green forever.
        """
        page = FakePage()
        scrape(page, observing)
        for action in (
            action_models.ClickAction(element_id="1"),
            action_models.InputTextAction(element_id="1", text="x"),
            action_models.ExtractAction(),
            action_models.GotoUrlAction(url=HOME),
        ):
            stamp_parsed_actions([action])
            self.replace_document(page)
            decision = preflight_action(action, page, site="test")
            assert decision is not None, action.action_type
            assert decision.outcome is PolicyOutcome.DENIED, action.action_type
            assert PolicyReason.UNVERIFIED_OBSERVATION in decision.reasons, action.action_type

    def test_only_the_recovery_set_survives_a_replaced_document(self, observing: SkyvernContext) -> None:
        # ADR-0011 keeps read-only and recovery actions available so a run can stop safely, and a
        # DOM swap does not make "wait" or "terminate" unsafe. This pins the exact surviving set so
        # nobody widens it by accident.
        page = FakePage()
        scrape(page, observing)
        allowed = []
        for model in CONCRETE_ACTIONS_FOR_SURVIVAL:
            action = model()
            stamp_parsed_actions([action])
            self.replace_document(page)
            decision = preflight_action(action, page, site="test")
            if decision is not None and decision.outcome is PolicyOutcome.ALLOWED:
                allowed.append(action.action_type)
        assert set(allowed) == {
            ActionType.WAIT,
            ActionType.SCROLL,
            ActionType.MOVE,
            ActionType.HOVER,
            ActionType.NULL_ACTION,
            ActionType.GO_BACK,
            ActionType.CLOSE_PAGE,
            ActionType.SWITCH_TAB,
            ActionType.TERMINATE,
            ActionType.COMPLETE,
        }

    def test_the_two_direct_verdict_seams_are_still_empty(self) -> None:
        """Watches the two DIRECT seams a scanner would touch, and claims nothing beyond them.

        The test above proves today's UNKNOWN behaviour through a fake epoch, which will keep
        supplying UNKNOWN no matter what production does. These assertions instead read production:
        the observation source takes no verdict, and the verdict in _live_observation is a literal.

        *** WHAT THIS DOES NOT COVER. *** A scanner that arrives by any other route — wrapping or
        replacing the observation after _live_observation returns, or setting the verdict further
        downstream — leaves both assertions true and this test green. Pinning an arbitrary future
        change is not achievable, and three attempts at widening this only implied coverage it did
        not have. So: when SKY-12526's scanner lands, the undetectable same-document replacement gap
        described on this class must be RE-DECIDED deliberately. It must not be assumed handled
        because this test still passes.
        """
        assert "verdict" not in inspect.signature(advance_observation_epoch).parameters

        source = ast.parse(Path("skyvern/forge/sdk/browser_action_preflight.py").read_text())
        observation = next(
            node for node in ast.walk(source) if isinstance(node, ast.FunctionDef) and node.name == "_live_observation"
        )
        verdicts = [
            ast.unparse(keyword.value)
            for node in ast.walk(observation)
            if isinstance(node, ast.Call)
            for keyword in node.keywords
            if keyword.arg == "verdict"
        ]
        assert verdicts == ["ObservationVerdict.UNKNOWN"]

    def test_the_epoch_does_not_keep_the_page_alive(self, observing: SkyvernContext) -> None:
        # The weakref is the liveness signal; anything capturing the page strongly would defeat it.
        page = FakePage()
        scrape(page, observing)
        epoch = observing.browser_observation_epoch
        assert epoch is not None and epoch.page is not None
        del page
        gc.collect()
        assert epoch.page() is None
        assert observed_page() is None

    def test_nothing_registers_a_listener_on_the_page(self, observing: SkyvernContext) -> None:
        # A listener is per-page state that outlives the run context that created it, so it leaks
        # across contexts on a persistent page. The module registers none.
        page = FakePage()
        for _ in range(5):
            scrape(page, observing)
            preflight_action(action_models.ClickAction(element_id="1"), page, site="test")
        assert page.listeners == {}


class TestContentBoundProvenance:
    """F3. The epoch counter is positional; a stray integer can match it by luck. The digest cannot."""

    def test_an_epoch_that_matches_by_value_alone_is_not_provenance(self, observing: SkyvernContext) -> None:
        page = FakePage()
        scrape(page, observing)
        epoch = observing.browser_observation_epoch
        assert epoch is not None

        # Exactly the rehydrated-integer collision: the number is right, the observation is not.
        action = action_models.ClickAction(element_id="1")
        action.observation_epoch = epoch.epoch

        decision = preflight_action(action, page, site="test")
        assert decision is not None
        assert PolicyReason.MISSING_PAGE_EVIDENCE in decision.reasons

    def test_a_correctly_stamped_action_is_accepted(self, observing: SkyvernContext) -> None:
        # The twin: without it the test above could be passing for an unrelated reason.
        page = FakePage()
        scrape(page, observing)
        action = action_models.ClickAction(element_id="1")
        stamp_parsed_actions([action])
        decision = preflight_action(action, page, site="test")
        assert decision is not None
        assert PolicyReason.MISSING_PAGE_EVIDENCE not in decision.reasons
        assert PolicyReason.STALE_PAGE_EVIDENCE not in decision.reasons


class TestInternalFailuresAreVisible:
    """Swallowing is required — observe mode may never change execution — but silence is not, and
    neither is a decision line that is safe on only one of its two emitters."""

    def test_a_swallowed_failure_still_emits_a_decision_event(self, observing: SkyvernContext) -> None:
        page = FakePage()
        scrape(page, observing)
        emitted: list[dict] = []
        with patch.object(preflight.LOG, "warning", lambda _event, **kwargs: emitted.append(kwargs)):
            with patch("skyvern.forge.sdk.browser_action_preflight.decide_browser_action", side_effect=RuntimeError):
                assert preflight_action(action_models.ClickAction(element_id="1"), page, site="test") is None
        assert len(emitted) == 1
        assert emitted[0]["outcome"] == preflight.INTERNAL_ERROR_OUTCOME
        assert emitted[0]["error_type"] == "RuntimeError"
        # Still enough to find the fault: source identifiers, which are code and not data.
        assert emitted[0]["error_location"].endswith(":preflight_action") or ".py:" in emitted[0]["error_location"]

    def test_an_exception_message_never_reaches_a_decision_line(self, observing: SkyvernContext) -> None:
        """An exception's text is arbitrary and routinely carries the data it choked on.

        This is the internal-fault sink, which is a SECOND emitter of the decision event. AC8 was
        verified on the normal emitter alone, and a guarantee that holds on one of two sinks is not
        a guarantee.
        """
        page = FakePage()
        scrape(page, observing)
        secret = "https://example.test/p?signature=SECRETVALUE9"
        emitted: list[dict] = []
        with patch.object(preflight.LOG, "warning", lambda event, **kwargs: emitted.append({"event": event, **kwargs})):
            with patch(
                "skyvern.forge.sdk.browser_action_preflight.decide_browser_action",
                side_effect=RuntimeError(f"failed {secret}"),
            ):
                preflight_action(action_models.ClickAction(element_id="1"), page, site="test")
        assert len(emitted) == 1
        assert "SECRETVALUE9" not in repr(emitted[0])
        # exc_info would format the exception text downstream of anything this module controls.
        assert "exc_info" not in emitted[0]

    # Every argument expression any LOG call in this module is permitted to pass. This is the whole
    # permitted set: anything not listed fails, including expressions that look harmless. Adding a
    # field to a log line therefore requires a human to add it here and say why.
    #
    # An ALLOWLIST because three previous versions of this check were name-matchers and each one was
    # evaded by a name a future edit was free to choose differently: a key allowlist in the
    # redactor, then event-name scoping that missed two sibling sinks, then variable-name matching
    # that missed `exc`, one hop of indirection, and LOG.exception's implicit exc_info. Following
    # the value rather than hunting for forbidden names is the same fix, applied to the test.
    PERMITTED_LOG_ARGUMENTS = {
        "DECISION_EVENT",
        "INTERNAL_ERROR_OUTCOME",
        "'Failed to advance the browser action observation epoch'",
        "'Failed to stamp actions with an observation epoch'",
        "site",
        "[]",
        "[reason.value for reason in decision.reasons]",
        "decision.outcome.value",
        "action.action_type",
        "action.observation_epoch",
        "None if observation is None else observation.observation_epoch",
        "None if origin is None else origin.canonical",
        "type(error).__name__",
        "_error_location(error)",
    }
    # info and warning take exc_info only when asked. LOG.exception supplies it IMPLICITLY, which is
    # why it is barred outright rather than checked for a kwarg — swapping warning for exception in
    # a handler is a one-word edit that reads as harmless.
    PERMITTED_LOG_METHODS = {"info", "warning"}
    LOG_CALL_COUNT = 4

    @staticmethod
    def _log_calls() -> list:
        source = ast.parse(Path("skyvern/forge/sdk/browser_action_preflight.py").read_text())
        return [
            node
            for node in ast.walk(source)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "LOG"
        ]

    def test_every_log_argument_in_the_module_is_on_the_allowlist(self) -> None:
        """A regression guard over the direct call shape. NOT a proof that leaking is impossible.

        CAUGHT: a denylist of {exc_info, error=} missed `error_type=str(error)`; before that the
        check was scoped to one event name and missed two sibling sinks entirely.

        *** WHAT IT DOES CATCH, verified by mutation rather than claimed. *** Every one of these was
        run against it and failed the suite: `exc_info=True` restored at any of the three fault
        sinks; an exception passed positionally; passed as `error=error`; stringified as
        `str(error)`; stringified as `repr(error)` one hop away; `str(exc)` under a different
        variable name; `LOG.exception` in place of `LOG.warning`; `getattr(LOG, "warning")`; a fifth
        LOG call appearing anywhere in the module; and the event string inlined at an emitter to
        escape a name-scoped scan. It also failed on its own first version, where a substring check
        for `LOG.exception` matched the comment explaining why `LOG.exception` is barred.

        *** WHAT THIS DOES NOT CATCH. *** It records each argument's VALUE expression, not the
        keyword it was passed under, and it only sees `LOG.<method>(...)` written literally in this
        module. All four of these defeat it and were confirmed to defeat it:

          * a permitted value passed under a dangerous keyword — `exc_info=DECISION_EVENT`, which
            structlog reads as a truthy request for the active traceback;
          * a logger alias — `AUDIT = LOG` then `AUDIT.exception(...)`;
          * a second logger from `structlog.get_logger()` inside a module helper;
          * logging delegated to a helper in another module.

        This is the fourth iteration of this check. The first three were beaten by a key name, an
        event name and a variable name, each fix correct and each defeated one order up — so the
        claim stops growing here rather than inviting a fifth version for a third-order trick. A
        guard that states its limits beats one that advertises coverage it lacks.

        THE LOAD-BEARING ASSURANCE IS ELSEWHERE: the four committed emitters are verified safe by
        behavioural probes against the real JSON pipeline, not by this scan. Full static enforcement
        is a follow-up ticket.
        """
        calls = self._log_calls()
        assert len(calls) == self.LOG_CALL_COUNT, "LOG call count changed — the scan or the module did"
        used = set()
        for call in calls:
            for argument in list(call.args) + [keyword.value for keyword in call.keywords]:
                used.add(ast.unparse(argument))
        assert used <= self.PERMITTED_LOG_ARGUMENTS, used - self.PERMITTED_LOG_ARGUMENTS

    def test_no_log_method_that_carries_a_traceback_implicitly(self) -> None:
        """CAUGHT: LOG.exception passes exc_info without naming it, so a kwarg check never sees it."""
        source = ast.parse(Path("skyvern/forge/sdk/browser_action_preflight.py").read_text())
        # Structural, not a substring scan: a comment mentioning LOG.exception is not a call, and a
        # text match on it fails on this module's own documentation. That is the same fragility this
        # test exists to guard against, one level up.
        attributes = {
            node.attr
            for node in ast.walk(source)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "LOG"
        }
        assert attributes <= self.PERMITTED_LOG_METHODS, attributes - self.PERMITTED_LOG_METHODS
        # An alias or a dynamic lookup would move the call out of the AST shape scanned above.
        lookups = [
            node
            for node in ast.walk(source)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and any(isinstance(arg, ast.Name) and arg.id == "LOG" for arg in node.args)
        ]
        assert lookups == []

    def test_the_module_has_no_other_way_to_emit(self) -> None:
        """Pins today's emission surface: the scans above see `LOG.<method>(...)` and nothing else.

        Narrow by construction — it checks imports, assignments and `print`, so it catches a stdlib
        logger or a `warnings` call appearing, and does NOT catch an alias of the existing LOG or a
        `structlog.get_logger()` helper. See the allowlist test above for the full boundary.
        """
        source = ast.parse(Path("skyvern/forge/sdk/browser_action_preflight.py").read_text())
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(source)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        assert not imported & {"logging", "warnings", "sys", "traceback", "linecache"}
        assignments = {
            target.id
            for node in ast.walk(source)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        assert "LOG" in assignments
        text = Path("skyvern/forge/sdk/browser_action_preflight.py").read_text()
        assert "print(" not in text

    def test_the_decision_event_has_more_than_one_emitter_and_all_are_covered(self) -> None:
        """CAUGHT: AC8 was verified on the decision sink while a second, unsafe emitter of the same
        event sat beside it.

        The weakest of the three and deliberately kept. Its non-vacuity role is now covered by the
        module-wide allowlist above, which does not depend on the event name; what survives is an
        executable record that this event has more than one emitter, so the next person verifying
        "the decision line is safe" checks all of them rather than the one they are looking at.
        """
        emitters = [
            call for call in self._log_calls() if any(ast.unparse(arg) == "DECISION_EVENT" for arg in call.args)
        ]
        assert len(emitters) >= 2, "expected the decision and internal-fault sinks — the scan is broken"

    def test_the_event_string_cannot_be_inlined_to_escape_the_scan(self) -> None:
        """CAUGHT: inlining the literal at an emitter escapes any name-scoped scan.

        Independently useful beyond leak-safety: one source for the event name means every emitter
        is findable by grepping a single constant, which is what would have surfaced the second sink
        immediately instead of several review rounds later.
        """
        source = Path("skyvern/forge/sdk/browser_action_preflight.py").read_text()
        assert source.count('"Browser action policy decision"') == 1

    def test_the_error_path_cannot_itself_raise(self, observing: SkyvernContext) -> None:
        """The error path needed its own error path tested.

        The battery proved exception TEXT cannot reach a log; nothing proved the code that formats
        the fault cannot raise out of the handler that must swallow it. `_error_location` used
        traceback.extract_tb, which resolves source lines through linecache and can invoke a module
        loader whose failure it does not catch — so the helper added to make the error path safe
        could end the caller while handling an internal fault.
        """
        page = FakePage()
        scrape(page, observing)
        action = action_models.ClickAction(element_id="1")
        stamp_parsed_actions([action])
        with patch.object(preflight, "_error_location", side_effect=RuntimeError("helper exploded")):
            with patch.object(preflight, "decide_browser_action", side_effect=RuntimeError("inner")):
                assert preflight_action(action, page, site="test") is None

    def test_the_location_helper_never_raises_whatever_it_is_handed(self) -> None:
        class Hostile(Exception):
            @property
            def __traceback__(self):  # type: ignore[override]
                raise RuntimeError("boom")

        assert preflight._error_location(Hostile()) is None
        assert preflight._error_location(ValueError("no traceback")) is None

    def test_an_authority_of_none_is_rejected_at_construction(self) -> None:
        # Omission already raised. This is the other way in.
        with pytest.raises(TypeError):
            BrowserActionRequest(
                policy=POLICY,
                projection=ActionProjection(action_type=None, action_class=None, target=ActionTarget()),
                authority=None,  # type: ignore[arg-type]
                request_epoch=1,
            )


class TestTransientObservationIdentity:
    def test_a_planned_batch_carries_the_current_epoch(self, observing: SkyvernContext) -> None:
        page = FakePage()
        scrape(page, observing)
        batch = [action_models.ClickAction(element_id="1"), action_models.WaitAction()]
        stamp_parsed_actions(batch)
        assert [action.observation_epoch for action in batch] == [1, 1]

    def test_the_observation_epoch_never_reaches_the_persisted_payload(self) -> None:
        # create_action writes action.model_dump() straight into action_json. An epoch is run-local,
        # so a value read back from a stored row could collide with a live run's counter and make a
        # stale action look freshly observed. This is the guard on `exclude=True`.
        action = action_models.ClickAction(element_id="1")
        action.observation_epoch = 3
        assert "observation_epoch" not in action.model_dump()
        assert "observation_epoch" not in action.model_dump(mode="json")
        assert action.observation_epoch == 3

    def test_a_derived_action_inherits_its_parent_identity(self, observing: SkyvernContext) -> None:
        page = FakePage()
        scrape(page, observing)
        parent = action_models.ClickAction(element_id="1", file_url=HOME)
        stamp_parsed_actions([parent])
        derived = action_models.UploadFileAction(element_id="1", file_url=HOME)
        preflight_derived_action(derived, page, parent=parent, site="test")
        assert derived.observation_epoch == parent.observation_epoch

    def test_a_derived_action_is_judged_on_its_own_projection(self, observing: SkyvernContext) -> None:
        # The whole point of re-projecting: parent and child do not require the same things.
        page = FakePage()
        scrape(page, observing)
        parent = action_models.ClickAction(element_id="1")
        stamp_parsed_actions([parent])
        derived = action_models.UploadFileAction(element_id="1", file_url=HOME)
        decision = preflight_derived_action(derived, page, parent=parent, site="test")
        assert decision is not None
        assert PolicyReason.MISSING_PROTECTED_REFERENCE in decision.reasons


class TestPolicyObservationIsIndependentOfPromptScanning:
    def test_observation_runs_with_no_scanner_wired(self, observing: SkyvernContext) -> None:
        # AC7. No detector exists in this tree, and the policy still gets to look. The verdict it
        # gets is UNKNOWN, which denies — fail-closed, not silently skipped.
        page = FakePage()
        scrape(page, observing)
        action = action_models.ClickAction(element_id="1")
        stamp_parsed_actions([action])
        decision = preflight_action(action, page, site="test")
        assert decision is not None
        assert PolicyReason.UNVERIFIED_OBSERVATION in decision.reasons

    def test_an_unenrolled_run_reaches_not_enrolled_and_nothing_else(self, observing: SkyvernContext) -> None:
        observing.browser_action_policy = None
        page = FakePage()
        scrape(page, observing)
        action = action_models.ClickAction(element_id="1")
        stamp_parsed_actions([action])
        decision = preflight_action(action, page, site="test")
        assert decision is not None
        assert decision.outcome is PolicyOutcome.NOT_ENROLLED


class TestEmptyRuntimeAuthoritySeam:
    """SKY-12883/12884/12886 fill the authority seam. Until they do it denies, loudly."""

    def test_an_enrolled_run_has_no_runtime_authority_today(self, observing: SkyvernContext) -> None:
        assert observing.browser_action_authority.state is AuthorityState.UNWIRED

    def test_being_inside_the_enrolled_ceiling_does_not_allow(self, observing: SkyvernContext) -> None:
        # Page origin IS enrolled and the observation IS current; the only thing left to deny on is
        # the absent authority. Without those two pins this would pass against any implementation.
        page = FakePage()
        scrape(page, observing)
        action = action_models.GotoUrlAction(url=HOME)
        stamp_parsed_actions([action])
        decision = preflight_action(action, page, site="test")
        assert decision is not None
        assert decision.outcome is PolicyOutcome.DENIED
        assert PolicyReason.UNWIRED_RUNTIME_AUTHORITY in decision.reasons

    def test_the_same_action_allows_once_an_authority_exists(self, observing: SkyvernContext) -> None:
        # The twin. If this fails, the deny above proves nothing about the authority gate.
        observing.browser_action_authority = ESTABLISHED
        page = FakePage()
        scrape(page, observing)
        action = action_models.GoBackAction()
        stamp_parsed_actions([action])
        decision = preflight_action(action, page, site="test")
        assert decision is not None
        assert decision.outcome is PolicyOutcome.ALLOWED


class TestBatchPreflight:
    """AC3: the complete batch, after WAIT filtering, before persistence/callback/sleep/execution."""

    @staticmethod
    async def _run(actions: list, spy) -> list:
        seen: list = []

        def capture(batch, *, site: str) -> None:
            seen.extend(spy(list(batch), site))
            raise _Reached(site)

        browser_state = MagicMock()
        browser_state.must_get_working_page = AsyncMock(return_value=FakePage())
        tracker = MagicMock()
        tracker.drain = AsyncMock()
        output = MagicMock()
        with patch("skyvern.forge.agent.preflight_batch", capture):
            with pytest.raises(_Reached):
                await ForgeAgent._execute_step_actions(
                    MagicMock(),
                    task=MagicMock(),
                    step=MagicMock(),
                    browser_state=browser_state,
                    engine=MagicMock(),
                    scraped_page=MagicMock(),
                    complete_verification=False,
                    actions=actions,
                    detailed_agent_step_output=output,
                    pdf_auto_download_src=None,
                    pdf_auto_download_used_bytes=False,
                    file_download_false_click_eligible=False,
                    _step_span=MagicMock(),
                    artifact_tracker=tracker,
                )
        # F1: the preflight must not have reached into browser state to find a page.
        browser_state.must_get_working_page.assert_not_awaited()
        return seen

    @pytest.mark.asyncio
    async def test_the_batch_is_evaluated_after_wait_filtering(self) -> None:
        batch = [action_models.WaitAction(), action_models.ClickAction(element_id="1")]
        seen = await self._run(batch, lambda actions, site: [(a.action_type, site) for a in actions])
        # If the call were placed before the WAIT filter, the skipped WAIT would show up here.
        assert seen == [(ActionType.CLICK, "step_action_batch")]

    @pytest.mark.asyncio
    async def test_the_batch_is_evaluated_before_anything_executes_or_persists(self) -> None:
        mock_app = MagicMock()
        with patch("skyvern.webeye.actions.handler.ActionHandler.handle_action") as handle:
            with patch("skyvern.forge.agent.app", mock_app):
                await self._run([action_models.ClickAction(element_id="1")], lambda actions, site: [site])
        handle.assert_not_called()
        mock_app.DATABASE.workflow_params.create_action.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_wait_only_batch_is_still_evaluated(self) -> None:
        seen = await self._run([action_models.WaitAction()], lambda actions, site: [a.action_type for a in actions])
        assert seen == [ActionType.WAIT]

    @pytest.mark.asyncio
    async def test_the_batch_preflight_does_not_stamp(self, observing: SkyvernContext) -> None:
        # F2: stamping belongs at the parse point. An action that reached this batch by another
        # route must not pick up provenance on the way past.
        page = FakePage()
        scrape(page, observing)
        action = action_models.ClickAction(element_id="1")
        preflight_batch([action], site="test")
        assert action.observation_epoch is None
        assert action.observation_digest is None


class TestPerActionPreflight:
    """AC4: re-evaluated in the concrete handler, before browser-manager lookup or persistence."""

    @pytest.mark.asyncio
    async def test_handle_action_preflights_before_the_browser_manager_lookup(self) -> None:
        seen: list[str] = []

        def capture(action, page, *, site: str) -> None:
            seen.append(site)
            raise _Reached(site)

        mock_app = MagicMock()
        with patch("skyvern.webeye.actions.handler.preflight_action", capture):
            with patch("skyvern.webeye.actions.handler.app", mock_app):
                with pytest.raises(_Reached):
                    await ActionHandler.handle_action(
                        scraped_page=MagicMock(),
                        task=MagicMock(),
                        step=MagicMock(),
                        page=FakePage(),
                        action=action_models.ClickAction(element_id="1"),
                    )
        assert seen == ["handle_action"]
        mock_app.BROWSER_MANAGER.get_for_task.assert_not_called()
        mock_app.DATABASE.workflow_params.create_action.assert_not_called()


class TestReprojectionOfDerivedActions:
    """AC5. The COMPLETE->TERMINATE conversion is driven for real because its ordering is the subtle
    part; the remaining conversions are pinned structurally below."""

    @pytest.mark.asyncio
    async def test_complete_to_terminate_is_reprojected_before_the_terminate_runs(self) -> None:
        order: list[str] = []

        async def fake_complete_verify(*_args, **_kwargs):
            result = MagicMock()
            result.is_terminate = True
            result.thoughts = "stop"
            result.status = None
            return result

        async def fake_terminate(*_args, **_kwargs):
            order.append("terminate")
            return []

        def capture(derived, page, *, parent, site: str) -> None:
            order.append(f"preflight:{site}:{derived.action_type}")

        action = action_models.CompleteAction(verified=False)
        with patch("skyvern.webeye.actions.handler.app.agent.complete_verify", side_effect=fake_complete_verify):
            with patch("skyvern.webeye.actions.handler.handle_terminate_action", side_effect=fake_terminate):
                with patch("skyvern.webeye.actions.handler.preflight_derived_action", capture):
                    from skyvern.webeye.actions.handler import handle_complete_action

                    await handle_complete_action(action, FakePage(), MagicMock(), MagicMock(), MagicMock())

        assert order == [f"preflight:complete_to_terminate:{ActionType.TERMINATE}", "terminate"]
        # And the in-place rewrite still happens, so nothing about execution changed.
        assert action.action_type == ActionType.TERMINATE

    def test_every_conversion_site_is_wired_to_the_right_preflight(self) -> None:
        wired = _preflight_calls(Path("skyvern/webeye/actions/handler.py")) | _preflight_calls(
            Path("skyvern/forge/agent.py")
        )
        assert wired == {
            # Judged as a batch; stamped earlier, at the point it was parsed out of the scrape.
            "step_action_batch": "preflight_batch",
            # Synthesized by the runtime after a reload. Judged, never stamped — stamping happens
            # only at the parse point, which this action never reaches.
            "internal_refresh": "preflight_action",
            "handle_action": "preflight_action",
            "click_to_upload": "preflight_derived_action",
            "upload_to_click": "preflight_action",
            "select_option_to_click": "preflight_derived_action",
            "select_option_to_checkbox": "preflight_derived_action",
            "complete_to_terminate": "preflight_derived_action",
        }


class TestObserveModeIsNotBehavioural:
    def test_a_preflight_failure_is_swallowed(self, observing: SkyvernContext) -> None:
        page = FakePage()
        scrape(page, observing)
        with patch("skyvern.forge.sdk.browser_action_preflight.decide_browser_action", side_effect=RuntimeError):
            assert preflight_action(action_models.ClickAction(element_id="1"), page, site="test") is None

    def test_an_unexpected_page_fault_does_not_escape(self, observing: SkyvernContext) -> None:
        broken = ExplodingPage()
        scrape(broken, observing)
        action = action_models.ClickAction(element_id="1")
        stamp_parsed_actions([action])
        assert preflight_action(action, broken, site="test") is None

    def test_a_runtime_synthesized_action_is_never_stamped(self, observing: SkyvernContext) -> None:
        # F2: only _generate_step_actions stamps, and an injected or synthesized action never
        # passes through it. Stamping one would hand it a page nobody scraped since.
        page = FakePage()
        scrape(page, observing)
        action = action_models.ReloadPageAction()
        preflight_action(action, observed_page(), site="test")
        assert action.observation_epoch is None
        assert action.observation_digest is None

    def test_no_observable_page_yields_a_decision_not_a_mutation(self, observing: SkyvernContext) -> None:
        # F1: when nothing can be observed the answer is a denial with a recorded reason, never a
        # reach into browser state to produce a page.
        assert observed_page() is None
        decision = preflight_action(action_models.ClickAction(element_id="1"), None, site="test")
        assert decision is not None
        assert PolicyReason.MISSING_PAGE_EVIDENCE in decision.reasons

    def test_advancing_the_epoch_outside_a_run_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "BROWSER_ACTION_POLICY_MODE", "observe")
        skyvern_context.reset()
        assert skyvern_context.current() is None
        advance_observation_epoch(FakePage(), main_frame_url=HOME, element_hashes=())
        stamp_parsed_actions([action_models.ClickAction(element_id="1")])

    def test_decisions_carry_no_page_content(self, observing: SkyvernContext) -> None:
        # AC8: non-sensitive. A full URL can carry customer data in its query string; the canonical
        # origin is what ADR-0011's monitoring plan actually asks for.
        page = FakePage(url="https://example.com/apply?ssn=123-45-6789&name=Someone")
        scrape(page, observing)
        action = action_models.ClickAction(element_id="1")
        stamp_parsed_actions([action])
        emitted: list[dict] = []
        with patch.object(preflight.LOG, "info", lambda _event, **kwargs: emitted.append(kwargs)):
            preflight_action(action, page, site="test")
        assert len(emitted) == 1
        rendered = repr(emitted[0])
        assert "ssn" not in rendered
        assert "123-45-6789" not in rendered
        assert emitted[0]["page_origin"] == declare_origin(HOME).canonical


class _Reached(Exception):
    """Raised by a spy so a test can prove nothing downstream of it ran."""


def _preflight_calls(path: Path) -> dict[str, str]:
    """Every preflight call in a module as `site` -> the function it calls, read from its AST.

    Carrying the function name and not just the site is what makes this catch a swap as well as a
    deletion: `preflight_planned_batch` stamps and `preflight_unplanned_action` does not, so using
    the wrong one at a site is a fail-open that leaves the site name untouched. Fails loudly rather
    than silently matching nothing: callers assert against an exact expected mapping.
    """
    tree = ast.parse(path.read_text())
    calls: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if not node.func.id.startswith("preflight_"):
            continue
        for keyword in node.keywords:
            if keyword.arg == "site" and isinstance(keyword.value, ast.Constant):
                calls[keyword.value.value] = node.func.id
    return calls
