from __future__ import annotations

import asyncio
import builtins
import html
import re
from bisect import bisect_left, bisect_right
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Callable, Iterable, Iterator, TypedDict
from zoneinfo import ZoneInfo

import structlog
from pydantic import AnyHttpUrl, TypeAdapter, ValidationError

from skyvern.config import settings
from skyvern.schemas.run_enums import RunEngine
from skyvern.webeye.browser_health import BrowserHealth, BrowserOperation

if TYPE_CHECKING:
    from playwright.async_api import FileChooser, Frame, Page

    from skyvern.forge.sdk.browser_action_policy import BrowserActionPolicy, RuntimeOriginAuthority
    from skyvern.forge.sdk.browser_action_preflight import ObservationEpoch, ObservedTabs
    from skyvern.forge.sdk.db.enums import WorkflowRunTriggerType

    # Deferred import: skyvern_context.py sits below the service layer and
    # must not pull a service module at import time. String annotation below.
    from skyvern.services.script_reviewer_v3.budget import RunBudget

LOG = structlog.get_logger()

# Cap on entries kept in `recent_dialog_messages` so a chatty page (e.g. validation
# alerts firing on every keystroke) cannot inflate the next prompt unboundedly.
MAX_RECENT_DIALOG_MESSAGES = 5
# Per-message length cap so a single pathological alert (multi-KB page-stack
# trace, etc.) cannot dominate the prompt budget.
MAX_DIALOG_MESSAGE_CHARS = 500

# Visible stand-in for a value scrubbed from the model's view via hide_from_model.
MODEL_HIDDEN_PLACEHOLDER = "[withheld: sign-in link]"


# An apostrophe is legal inside a URL, so it is part of the span; a prose quote is trimmed with the
# other trailing punctuation below.
URL_IN_TEXT = re.compile(r"https?://[^\s<>\"`]+", re.IGNORECASE)
_URL_START = re.compile(r"https?://", re.IGNORECASE)
_URL_TRAILING_PUNCTUATION = ".,;:!?)]}>'\""
_PERCENT_ESCAPE_RE = re.compile(r"%[0-9a-f]{2}", re.IGNORECASE)
# Upper bound on prose punctuation or quotes tried around one URL span, so a page-controlled run of
# punctuation cannot make the pass quadratic.
_MAX_SPAN_CUTS = 8
# The same WHATWG parser validate_fetch_url runs before page.goto, so an echoed URL is compared in the
# exact shape the browser was handed (host case/IDN/IP literal, default port, dot segments, path chars).
_BROWSER_URL = TypeAdapter(AnyHttpUrl)
# Chromium additionally percent-encodes these two path characters that the spec parser keeps raw.
_CHROMIUM_PATH_ESCAPES = str.maketrans({"^": "%5E", "|": "%7C"})
_URL_PATH_RE = re.compile(r"^(?P<prefix>[a-z][a-z0-9+.-]*://[^/?#]*)(?P<path>[^?#]*)", re.IGNORECASE)


def canonical_url(url: str) -> str:
    """The identity of a URL as the browser reports it back: WHATWG-normalized, Chromium path escapes
    applied, percent-escape case folded. Comparison-only; a string the parser rejects is compared as
    written."""
    try:
        normalized = str(_BROWSER_URL.validate_python(url))
    except ValidationError:
        normalized = url
    normalized = _URL_PATH_RE.sub(
        lambda m: m.group("prefix") + m.group("path").translate(_CHROMIUM_PATH_ESCAPES), normalized, count=1
    )
    return _PERCENT_ESCAPE_RE.sub(lambda m: m.group(0).upper(), normalized)


def opaque_url_echo_forms(url: str) -> tuple[str, ...]:
    """Every shape a surface can hand back a payload URL in, and so every shape the masker matches: as
    minted, as the browser canonicalises it, and either one entity-escaped for HTML."""
    canonical = canonical_url(url)
    return tuple(dict.fromkeys((url, html.escape(url, quote=False), canonical, html.escape(canonical, quote=False))))


def opaque_url_echo_window(urls: Iterable[str]) -> int:
    """The longest text the masker recognises as one payload URL, in UTF-16 code units so a JS slice()
    measures it the same way; the slack covers the punctuation a span search trims around it."""
    return (
        max((len(form.encode("utf-16-le")) // 2 for url in urls for form in opaque_url_echo_forms(url)), default=0) + 16
    )


def _mask_url_span(raw: str, canonical: dict[str, str], window: int) -> str:
    """Rewrite every payload ref inside one URL-shaped span. Iterative and window-bounded, so a page
    that glues thousands of refs with quotes costs linear time and no stack."""
    quotes = [i for i, ch in enumerate(raw) if ch == "'"]
    out: list[str] = []
    pos = 0
    # Scheme-only search: matching the greedy span regex from each offset would rescan the tail.
    while (match := _URL_START.search(raw, pos)) is not None:
        start = match.start()
        out.append(raw[pos:start])
        # Candidate ends, longest first: the window edge and each quote in it (a ref that extends past a
        # quote wins over a ref that is its prefix; a ref whose own path holds many quotes is found from
        # the last few), each with a bounded amount of trailing punctuation trimmed.
        limit = min(len(raw), start + window)
        first, last = bisect_left(quotes, start), bisect_right(quotes, limit)
        stops = {*quotes[first : first + _MAX_SPAN_CUTS], *quotes[max(first, last - _MAX_SPAN_CUTS) : last], limit}
        ends: builtins.set[int] = builtins.set()
        for stop in stops:
            floor = stop
            while floor > start and raw[floor - 1] in _URL_TRAILING_PUNCTUATION:
                floor -= 1
            ends.update((stop, *range(floor, min(stop, floor + _MAX_SPAN_CUTS))))
        token = None
        for stop in sorted(ends, reverse=True):
            if stop <= start:
                continue
            span = raw[start:stop]
            # A span lifted out of HTML carries entity-escaped separators (&amp;); a ref never does, so
            # only the text side is decoded, and only after the raw span failed to match.
            for candidate in dict.fromkeys((span, html.unescape(span) if "&" in span else span)):
                token = canonical.get(canonical_url(candidate))
                if token is not None:
                    break
            if token is not None:
                out.append(token)
                pos = stop
                break
        else:
            out.append(raw[start])
            pos = start + 1
    out.append(raw[pos:])
    return "".join(out)


def mask_opaque_urls_in_text(text: str, refs: dict[str, str]) -> str:
    """Replace every occurrence of a known payload signed-URL in ``text`` with its opaque token — the
    inverse of resolving that token. Masking is by PROVENANCE (membership in ``refs``), never URL
    shape, so a live-page URL the model must reason about is untouched even when it is itself
    signing-shaped (a ``?gclid=``/``?token=`` landing page). ``refs`` maps token -> real URL (the
    OpaqueUrlRefs.refs shape). Same object when nothing matches."""
    if not refs:
        return text
    masked = text
    # Longest URL first so a payload URL that is a prefix of another is not partially rewritten.
    for token, url in sorted(refs.items(), key=lambda item: len(item[1]), reverse=True):
        # A URL rendered inside HTML (get_html) has its query separators entity-encoded (& -> &amp;),
        # so a multi-parameter presigned URL never matches its raw form there; match the escaped form
        # too. Plain-text surfaces carry only the raw form, where html.escape is a no-op.
        for variant in dict.fromkeys((url, html.escape(url, quote=False))):
            if variant in masked:
                masked = masked.replace(variant, token)
    # The browser reports a payload URL back in canonical form (page.url adds the "/" path, drops a
    # default port, punycodes a host), which no exact substring pass can anticipate; compare each
    # URL-shaped span of the text by canonical identity, still membership-only.
    if not URL_IN_TEXT.search(masked):
        return masked
    canonical = {canonical_url(url): token for token, url in refs.items()}
    window = opaque_url_echo_window(refs.values())
    rewritten = URL_IN_TEXT.sub(lambda m: _mask_url_span(m.group(0), canonical, window), masked)
    return masked if rewritten == masked else rewritten


def _unwired_authority() -> RuntimeOriginAuthority:
    # Same deferred-import reason as the TYPE_CHECKING block above: the policy core pulls the action
    # models, which this module must not import at module load.
    from skyvern.forge.sdk.browser_action_policy import UNWIRED_AUTHORITY

    return UNWIRED_AUTHORITY


class DialogEntry(TypedDict):
    type: str
    message: str
    count: int


class EnrichTreeMode(StrEnum):
    CONTROL = "control"
    ENRICHED_TREE = "enriched_tree"
    ENRICHED_TREE_NO_IMAGES = "enriched_tree_no_images"
    ENRICHED_TREE_NO_IMAGES_FALLBACK = "enriched_tree_no_images_fallback"


def parse_enrich_tree_mode(value: Any) -> EnrichTreeMode:
    if isinstance(value, EnrichTreeMode):
        return value
    if isinstance(value, str):
        try:
            return EnrichTreeMode(value)
        except ValueError:
            LOG.warning("Unknown enrich_tree mode value, defaulting to control", enrich_tree_mode=value)
    return EnrichTreeMode.CONTROL


@dataclass
class PendingFileChooserListener:
    page: Page
    file_paths: list[str] | str
    handler: Callable[[FileChooser], Any] | None = None
    triggered: bool = False

    def cleanup(self) -> None:
        if self.handler is not None:
            try:
                self.page.remove_listener("filechooser", self.handler)
            except Exception:
                LOG.debug("Failed to remove filechooser listener during cleanup", exc_info=True)
            self.handler = None


@dataclass
class SkyvernContext:
    request_id: str | None = None
    organization_id: str | None = None
    organization_name: str | None = None
    org_default_llm_key: str | None = None
    org_default_secondary_llm_key: str | None = None
    task_id: str | None = None
    step_id: str | None = None
    workflow_id: str | None = None
    workflow_permanent_id: str | None = None
    workflow_run_id: str | None = None
    root_workflow_run_id: str | None = None
    task_v2_id: str | None = None
    max_steps_override: int | None = None
    browser_session_id: str | None = None
    # Immutable lease identity returned by the successful PBS begin_session call. Consumers carry
    # both values forward; they never reconstruct ownership from mutable task or session rows.
    browser_session_runnable_id: str | None = None
    browser_session_runnable_generation_id: str | None = None
    # Set only by run_sdk_action when it mints a bookkeeping run for a standalone action. A minted
    # run never begins the browser session, so it must not be presented as the expected owner.
    workflow_run_is_synthetic: bool = False
    # Set by run_sdk_action for EVERY inline action, whether it minted the run or the caller supplied an
    # existing (possibly already-terminal) run id. The browser is driven directly by the caller across
    # calls, so a run-scoped external allocation under it must never be an owner-terminal early-reap
    # input — unlike workflow_run_is_synthetic, this stays true for the supplied-run reuse path too.
    is_sdk_inline_action: bool = False
    browser_runtime: str | None = None
    browser_address_is_server_assigned: bool = False
    browser_health: BrowserHealth = field(default_factory=BrowserHealth)
    tz_info: ZoneInfo | None = None
    run_id: str | None = None
    copilot_session_id: str | None = None
    # Set only by the in-process copilot block-test path, on that run's own context. A dispatched
    # run's context is rebuilt on the worker and never carries it, so runner selection can tell the
    # two apart instead of inferring it from a process-wide capability.
    copilot_inline_execution: bool = False
    # The CodeBlock arm the rollout assigned this run to ("secure_runner" / "legacy_in_process"),
    # stamped only on an authoritative flag/pin verdict so log lines can be grouped by arm for an
    # unbiased secure-vs-legacy comparison. Left None when no genuine assignment was made (no browser
    # session, provider unreachable) so a degraded provider never biases the legacy arm.
    codeblock_execution_path: str | None = None
    navigation_goal: str | None = None
    navigation_payload: dict[str, Any] | list | str | None = None
    complete_criterion_is_untrusted: bool = False
    download_suffix: str | None = None
    totp_codes: dict[str, str | None] = field(default_factory=dict)
    active_credential_parameter_key: str | None = None
    log: list[dict] = field(default_factory=list)
    hashed_href_map: dict[str, str] = field(default_factory=dict)
    # builtins.set, not set: the module-level `set` context setter below shadows the
    # builtin for anything that resolves the name after import.
    downloaded_pdf_sources: set[str] = field(default_factory=builtins.set)
    # Per-task secret values (e.g. a resolved verification code) to scrub from artifacts/logs. Task-
    # scoped so bare tasks with no workflow-run context are still redacted; unioned into
    # WorkflowContextManager.get_secret_values_for_run, which both redaction consumers read.
    runtime_secret_values: set[str] = field(default_factory=builtins.set)
    # Subset of runtime_secret_values that must also never reach the model's own view of tool
    # output (e.g. a magic sign-in link), as opposed to values the model needs to read (e.g. a TOTP
    # code) that are only scrubbed from artifacts/logs.
    model_hidden_values: set[str] = field(default_factory=builtins.set)
    # Signed payload URLs the v3 loop minted opaque tokens for (token -> real URL, mirroring
    # OpaqueUrlRefs.refs). Applied to every model-facing tool result by hide_from_model so a resolved
    # ref never re-enters the transcript verbatim. Keyed by membership, never URL shape. Replaced whole
    # per task on the assumption blocks run sequentially; a parallel block type must single-flight
    # this like workflow_block_engine_lock or one task's refs stomp another's mid-flight.
    opaque_url_refs: dict[str, str] = field(default_factory=dict)
    refresh_working_page: bool = False
    frame_index_map: dict[Frame, int] = field(default_factory=dict)
    dropped_css_svg_element_map: dict[str, bool] = field(default_factory=dict)
    max_screenshot_scrolls: int | None = None
    browser_container_ip: str | None = None
    browser_container_task_arn: str | None = None
    feature_flag_entries: dict[str, bool | str | None] = field(default_factory=dict)
    # Absolute event-loop time the run body's elapsed-time budget expires, set alongside the
    # asyncio.timeout that enforces it. None when nothing is enforcing one. Read by work that
    # may block for a long time, so it can give up and return rather than be cancelled — a
    # cancellation propagates as BaseException and skips handlers that degrade gracefully.
    max_elapsed_deadline: float | None = None

    # feature flags
    enable_page_ready_wait: bool = False
    use_prompt_caching: bool = False
    cached_static_prompt: str | None = None
    vertex_cache_name: str | None = None  # Vertex AI cache resource name for explicit caching
    vertex_cache_key: str | None = None  # Logical cache key (includes variant + llm key)
    vertex_cache_variant: str | None = None  # Variant identifier used when creating the cache
    prompt_caching_settings: dict[str, bool] | None = None
    # SKY-9718 Layer 1 — gates apply_lean_recipe in prompt_engine + agent.
    # PostHog flag ENABLE_LEAN_ELEMENT_TREE, evaluated once per run at scrape time
    # and read sync from prompt-build sites.
    enable_lean_element_tree: bool = False
    # PRESERVE_TRANSIENT_UI_CAPTURE experiment arm, resolved per run. Tri-state: True=treatment
    # (suppress a scroll that would dismiss an open transient popup), False=control (shadow-detect
    # only), None=off (undefined/no-provider/error -> current scrolling behavior).
    preserve_transient_ui_capture: bool | None = None
    # Pinned once resolve_transient_ui_capture_arm resolves the arm (including off/None), so a TTL
    # expiry or mid-run flag ramp cannot flip the arm later in the same run.
    preserve_transient_ui_capture_resolved: bool = False
    # Single-flight the first-use provider resolution when parallel blocks/branches share one
    # context, so it is queried at most once per run (mirrors slim_output_variant_lock).
    preserve_transient_ui_capture_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Count of CONSECUTIVE agent-step captures the treatment arm has suppressed scrolling on. Co-owned
    # by the two agent-step capture sites — the agent-step scrape (scrape_web_unsafe with
    # allow_transient_ui_suppression=True) and the post-action screenshot
    # (record_artifacts_after_action) — via decide_transient_ui_suppression: incremented when a
    # capture suppresses, reset to 0 when a qualifying popup is not detected, and frozen at the cap
    # while a stale expanded trigger keeps matching so later captures fall back to legacy scrolling.
    # Both sites for a run run sequentially, so the read-modify-write needs no lock; verification /
    # extraction / error-detection scrapes never touch it.
    transient_ui_consecutive_suppressions: int = 0
    # WORKFLOW_TASK_V3_AB arm, resolved once per workflow run: the engine every default-engine
    # task block of that run dispatches to, or None for control.
    workflow_block_engine_override: RunEngine | None = None
    # The workflow run the override above was resolved for. A nested execution sharing this context
    # (an inline child workflow run) has its own id and its own definition, so it must re-resolve
    # rather than inherit an arm that was never checked against its blocks.
    workflow_block_engine_resolved_run_id: str | None = None
    # Single-flight the first-use provider resolution when parallel branches share one context.
    workflow_block_engine_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    enrich_tree_mode: EnrichTreeMode = EnrichTreeMode.CONTROL
    step_retry_index: int = 0

    # Run-level SLIM_LLM_OUTPUT_PROMPTS assignment, resolved once by slim_llm_output.
    # The lock makes first-use resolution single-flight under parallel prompt builds.
    slim_output_variant_assigned: str | None = None
    slim_output_variant_resolved: bool = False
    slim_output_variant_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # Trigger type of the enclosing workflow run (manual/api/scheduled/webhook).
    # Routed through SkyvernContext so non-API entry points (workers, scripts) can populate it
    # without taking a dependency on the public-API request shape.
    trigger_type: WorkflowRunTriggerType | None = None

    # Screenshot attribution: set by the agent before calling scrape so the
    # scraper can tag screenshot spans with the originating workflow phase
    # and whether the LLM will consume the screenshots.
    scrape_trigger: str | None = None
    scrape_screenshots_consumed: bool | None = None
    # When true, downstream LLM handler selection may swap the resolved handler to a
    # flex-tier router. Cloud sets this at run boot via a PostHog flag for non-UI runs;
    # OSS keeps it False because OSS has no flex routers registered.
    use_flex_llm_routing: bool = False

    # script run context
    code_version: int | None = None
    script_id: str | None = None
    script_revision_id: str | None = None
    action_order: int = 0
    prompt: str | None = None
    parent_workflow_run_block_id: str | None = None
    workflow_run_block_id: str | None = None
    # Caller-selected block labels for a partial workflow run; None/empty = full run.
    run_block_labels: list[str] | None = None
    loop_metadata: dict[str, Any] | None = None
    loop_internal_state: dict[str, Any] | None = None
    loop_output_values: list[list[dict[str, Any]]] | None = None
    script_run_parameters: dict[str, Any] = field(default_factory=dict)
    script_mode: bool = False
    is_static_script: bool = False
    sensitive_values: set[str] = field(default_factory=builtins.set)
    ai_mode_override: str | None = None
    script_llm_call_count: int = 0
    last_classify_result: str | None = None
    last_classify_meta: dict[str, Any] | None = None
    current_step_actions: list[dict[str, Any]] | None = None
    skip_complete_verification: bool = False

    # Set by ValidationBlock.execute() for the duration of the block so the prompt builder
    # can drop page DOM/URL/screenshots from the validation prompt. Restored after the block.
    validation_without_page_information: bool = False

    # v3 agentic reviewer — per-run cumulative budget. Initialized at workflow
    # run start for v3-cohort workflows; None for v2-cohort runs. SKY-7676.
    v3_run_budget: RunBudget | None = None

    # magic link handling
    # task_id is the key, page is the value
    # we only consider the page is a magic link page in the same task scope
    # for example, login block has a magic link page,
    # but it will only be considered as a magic link page in the login block scope
    # next blocks won't consider the page as a magic link page
    magic_link_pages: dict[str, Page] = field(default_factory=dict)

    # Exact popup Page objects opened by a download click, keyed by the task that opened them. A
    # download credited after the action seam returns (the CDP monitor / file-scan task lifecycle,
    # which never fires a Playwright popup download event) can then close the never-committed marker
    # popup it stranded. Task-keyed and dropped on task teardown so a claim never leaks into a later
    # task/run/persistent-session scope.
    download_popup_claims: dict[str, list[Page]] = field(default_factory=dict)

    # parallel verification optimization
    # stores pre-scraped data for next step to avoid re-scraping
    next_step_pre_scraped_data: dict[str, Any] | None = None
    speculative_plans: dict[str, Any] = field(default_factory=dict)
    # Writes that persist the cost of an already-billed speculative LLM call. They are
    # started as background tasks so the completion path doesn't wait on the LLM call,
    # and drained at task clean-up so the write can't be dropped when the run tears down.
    pending_speculative_persist_tasks: list[asyncio.Task] = field(default_factory=list)

    """
    Example output value:
    {"loop_value": "str", "output_parameter": "the key of the parameter", "output_value": Any}
    """
    generate_script: bool = True
    action_ai_overrides: dict[str, dict[int, str]] = field(default_factory=dict)
    action_counters: dict[str, int] = field(default_factory=dict)

    # Track if script generation skipped any actions due to missing data (race condition)
    # Used to determine if finalize regeneration is needed at workflow completion
    script_gen_had_incomplete_actions: bool = False

    # Track task_ids where proactive captcha injection has already been attempted,
    # preventing repeated injection loops when the captcha solver succeeds but the page doesn't change
    proactive_captcha_task_ids: set[str] = field(default_factory=builtins.set)

    # Circuit breaker: consecutive captcha solve timeouts for this workflow run.
    # When this reaches the threshold, further captcha solve attempts are short-circuited.
    consecutive_captcha_timeouts: int = 0

    # Circuit breaker: repeated successful captcha solves for one identity (the solve-budget
    # key below). Bounds a solve-succeeds-repeatedly-but-run-never-advances loop that the
    # timeout counter cannot see. Independent from consecutive_captcha_timeouts above.
    consecutive_captcha_solves: int = 0
    # Solve-budget key (task id + exact page url + concrete solver identity) of the last
    # reliably-successful solve; a solve with a matching key spends one unit of the budget.
    # None means no such solve has run yet. Opaque comparison string only; never log it.
    last_captcha_solve_key: str | None = None
    # Fast-fail latch (task id + exact page url) set when the solve budget above trips, so a retry
    # of the captcha action short-circuits at the entry point before invoking a solver instead of
    # paying another vendor call to re-raise the same failure. Coarser than last_captcha_solve_key
    # (no solver identity — a pre-entry check can't know it without running the detector); a url
    # change re-opens it. None means not latched. Opaque comparison string only; never log it.
    captcha_solve_latch_key: str | None = None

    # Browser dialogs captured since the last agent prompt build, surfaced into the
    # next extract-action prompt so the LLM can react to validation rejections.
    recent_dialog_messages: list[DialogEntry] = field(default_factory=list)

    # Per-step prompt token breakdown (SKY-9718). Written by prompt-build sites
    # (prompt_engine.load_prompt_with_elements_tracked + the cached extract-action
    # path in agent.py); read + cleared by the LLM API handler when emitting the
    # "LLM API handler duration metrics" log so the locally-counted prompt size
    # lands alongside the provider's input_tokens / llm_cost on the same row.
    last_prompt_breakdown: dict[str, Any] | None = None

    # Deferred file chooser listener — survives across steps so a popup-intercepted upload
    # can be completed when a subsequent click triggers the actual file chooser.
    pending_file_chooser: PendingFileChooserListener | None = None

    # Browser action firewall (SKY-12873). Bound from the resolved workflow version before the run's
    # browser exists; None means unenrolled, which is the only state standalone SDK actions can be
    # in. Never copied from a parent context — a child workflow binds its own version's policy.
    #
    # Deliberately a plain attribute and not a property: the value must already be resolved before
    # the browser existed, and an accessor would invite lazy loading that reads it mid-run, letting
    # a control-plane replacement change a live run's authority.
    #
    # This is a CEILING, not the complete authority. It answers "is this run protected, and what did
    # an operator authorize at most" — not "what may this action reach right now". A consumer must
    # not treat within-the-enrolled-set as sufficient to allow.
    browser_action_policy: BrowserActionPolicy | None = None

    # What the run may reach *right now* (SKY-12874). The ceiling above says what an operator
    # authorized at most; this says what ADR-0011's task-URL-derived authority grants at this
    # moment, and an origin-gated action needs both.
    #
    # It is UNWIRED_AUTHORITY on every run today, because nothing derives an authority yet:
    # SKY-12883, SKY-12884 and SKY-12886 are the tickets that fill this slot. Until they land, no
    # code in this repository implements ADR-0011's "block until authority is established" or its
    # "permanently invalidate on loss or rotation after a browser context is bound". Enrollment
    # cannot stand in for either — a static origin set never goes missing and never rotates.
    browser_action_authority: RuntimeOriginAuthority = field(default_factory=_unwired_authority)

    # Newest accepted scrape (SKY-12874). Advanced by the scrape itself; actions are stamped with
    # the epoch they were planned under so an observation cannot vouch for a plan built before it.
    browser_observation_epoch: ObservationEpoch | None = None

    # The open-tab list exactly as the planner's prompt rendered it, bound to the epoch it was
    # rendered under (SKY-12875). A SwitchTabAction's tab_index resolves against this record and
    # nothing else; no record, or a record from another epoch, resolves nothing.
    browser_observed_tabs: ObservedTabs | None = None

    def set_enrich_tree_mode(self, mode: Any) -> None:
        self.enrich_tree_mode = parse_enrich_tree_mode(mode)

    def enriched_tree_enabled(self) -> bool:
        return self.enrich_tree_mode != EnrichTreeMode.CONTROL

    def enrich_tree_fallback_active(self, *, retry_index: int | None = None) -> bool:
        effective_retry_index = self.step_retry_index if retry_index is None else retry_index
        return self.enrich_tree_mode == EnrichTreeMode.ENRICHED_TREE_NO_IMAGES_FALLBACK and effective_retry_index > 0

    def llm_screenshots_enabled_for_prompt(
        self,
        *,
        is_vision_fallback_prompt: bool = False,
        retry_index: int | None = None,
    ) -> bool:
        if is_vision_fallback_prompt:
            return True

        mode = self.enrich_tree_mode
        if mode in {EnrichTreeMode.CONTROL, EnrichTreeMode.ENRICHED_TREE}:
            return True
        if mode == EnrichTreeMode.ENRICHED_TREE_NO_IMAGES:
            return False

        effective_retry_index = self.step_retry_index if retry_index is None else retry_index
        return effective_retry_index > 0

    def cleanup_pending_file_chooser(self) -> None:
        if self.pending_file_chooser is not None:
            if not self.pending_file_chooser.triggered:
                LOG.warning("Cleaning up unconsumed pending file chooser listener")
            self.pending_file_chooser.cleanup()
            self.pending_file_chooser = None

    def __repr__(self) -> str:
        return f"SkyvernContext(request_id={self.request_id}, organization_id={self.organization_id}, task_id={self.task_id}, step_id={self.step_id}, workflow_id={self.workflow_id}, workflow_run_id={self.workflow_run_id}, task_v2_id={self.task_v2_id}, max_steps_override={self.max_steps_override}, run_id={self.run_id}, copilot_session_id={self.copilot_session_id})"

    def __str__(self) -> str:
        return self.__repr__()

    def pop_totp_code(self, task_id: str) -> None:
        if task_id in self.totp_codes:
            self.totp_codes.pop(task_id)

    def register_secret_value(self, value: str | None, *, hide_from_model: bool = False) -> None:
        """Mark a value for redaction from this task's artifacts/logs (task-scoped, no workflow needed).
        When hide_from_model is True, also scrub it from the model's own view of tool output via hide_from_model()."""
        if value:
            self.runtime_secret_values.add(value)
            if hide_from_model:
                self.model_hidden_values.add(value)

    def hide_from_model(self, text: str) -> str:
        """Return ``text`` with everything that must not reach the model's view of tool output replaced
        by a model-safe surrogate: first each model_hidden_values entry by MODEL_HIDDEN_PLACEHOLDER
        (longest value first so a substring value can't fragment a longer one), then each known payload
        signed-URL by its opaque token (by PROVENANCE, not shape). Same object when nothing matches."""
        for value in sorted(self.model_hidden_values, key=len, reverse=True):
            if value and value in text:
                text = text.replace(value, MODEL_HIDDEN_PLACEHOLDER)
        return mask_opaque_urls_in_text(text, self.opaque_url_refs)

    def record_dialog_message(self, dialog_type: str, dialog_message: str) -> None:
        """Buffer a dialog with FIFO cap; identical entries bump a count instead of duplicating."""
        if not dialog_message:
            return
        if len(dialog_message) > MAX_DIALOG_MESSAGE_CHARS:
            dialog_message = dialog_message[:MAX_DIALOG_MESSAGE_CHARS] + "…"
        for entry in self.recent_dialog_messages:
            if entry["type"] == dialog_type and entry["message"] == dialog_message:
                entry["count"] += 1
                return
        self.recent_dialog_messages.append({"type": dialog_type, "message": dialog_message, "count": 1})
        if len(self.recent_dialog_messages) > MAX_RECENT_DIALOG_MESSAGES:
            del self.recent_dialog_messages[0]

    def format_recent_dialog_messages(self) -> str | None:
        """Render the buffered dialogs into prompt-ready text without clearing; None when empty."""
        if not self.recent_dialog_messages:
            return None
        lines: list[str] = []
        for entry in self.recent_dialog_messages:
            suffix = f" (x{entry['count']})" if entry["count"] > 1 else ""
            lines.append(f"[{entry['type']}{suffix}] {entry['message']}")
        return "\n".join(lines)

    def clear_recent_dialog_messages(self) -> None:
        """Drop the buffered dialogs once the prompt has consumed them."""
        self.recent_dialog_messages.clear()

    def add_magic_link_page(self, task_id: str, page: Page) -> None:
        self.magic_link_pages[task_id] = page

    def has_magic_link_page(self, task_id: str) -> bool:
        if task_id not in self.magic_link_pages:
            return False

        page = self.magic_link_pages[task_id]
        if page.is_closed():
            self.magic_link_pages.pop(task_id)
            return False
        return True

    def record_download_popup_claim(self, task_id: str, page: Page) -> None:
        claims = self.download_popup_claims.setdefault(task_id, [])
        if all(existing is not page for existing in claims):
            claims.append(page)

    def take_download_popup_claims(self, task_id: str) -> list[Page]:
        return self.download_popup_claims.pop(task_id, [])

    def clear_download_popup_claims(self, task_id: str) -> None:
        self.download_popup_claims.pop(task_id, None)

    def flush_feature_flags(self) -> None:
        if not self.feature_flag_entries:
            return

        has_workflow = bool(self.workflow_run_id)
        has_task = bool(self.task_id or self.task_v2_id or self.run_id)

        if not (has_workflow or has_task):
            LOG.debug(
                "Discarding feature flag entries for non-run context",
                count=len(self.feature_flag_entries),
            )
            self.feature_flag_entries.clear()
            return

        feature_resolutions = dict(sorted(self.feature_flag_entries.items()))
        log_fields: dict[str, Any] = {
            "organization_id": str(self.organization_id or ""),
            "feature_resolutions": feature_resolutions,
            "service_name": settings.OTEL_SERVICE_NAME,
        }
        if self.workflow_run_id:
            log_fields["workflow_run_id"] = str(self.workflow_run_id)
        if self.workflow_permanent_id:
            log_fields["workflow_permanent_id"] = str(self.workflow_permanent_id)
        if self.task_id:
            log_fields["task_id"] = str(self.task_id)
        if self.task_v2_id:
            log_fields["task_v2_id"] = str(self.task_v2_id)
        if self.run_id:
            log_fields["run_id"] = str(self.run_id)
        if self.browser_session_id:
            log_fields["browser_session_id"] = str(self.browser_session_id)
        if self.request_id:
            log_fields["request_id"] = str(self.request_id)

        event_name = "workflow_feature_flags" if has_workflow else "task_feature_flags"
        LOG.info(event_name, **log_fields)
        self.feature_flag_entries.clear()


_context: ContextVar[SkyvernContext | None] = ContextVar(
    "Global context",
    default=None,
)


def current() -> SkyvernContext | None:
    """
    Get the current context

    Returns:
        The current context, or None if there is none
    """
    return _context.get()


def ensure_context() -> SkyvernContext:
    """
    Get the current context, or raise an error if there is none

    Returns:
        The current context if there is one

    Raises:
        RuntimeError: If there is no current context
    """
    context = current()
    if context is None:
        raise RuntimeError("No skyvern context")
    return context


def record_browser_timeout(operation: BrowserOperation) -> None:
    """Note that a browser-protocol operation went unanswered. Outside a run there is nothing to
    tally against, and callers are hot paths, so a missing context is silently a no-op."""
    context = current()
    if context is not None:
        context.browser_health.record_timeout(operation)


def record_browser_success() -> None:
    context = current()
    if context is not None:
        context.browser_health.record_success()


def record_browser_recovery(operation: BrowserOperation) -> None:
    context = current()
    if context is not None:
        context.browser_health.record_recovery(operation)


def set(context: SkyvernContext) -> None:
    """
    Set the current context

    Args:
        context: The context to set

    Returns:
        None
    """
    _context.set(context)


def replace(context: SkyvernContext) -> None:
    """
    Flush the current context summary, then replace it with a new context.

    Args:
        context: The context to set

    Returns:
        None
    """
    _cleanup_outgoing_context(current())
    _context.set(context)


def _cleanup_outgoing_context(context: SkyvernContext | None) -> None:
    if context is None:
        return
    if context.feature_flag_entries:
        context.flush_feature_flags()
    context.cleanup_pending_file_chooser()


def _restore(token: Token[SkyvernContext | None]) -> None:
    """
    Flush the current context summary and restore the previous context using a token.

    Args:
        token: ContextVar token returned by ContextVar.set()

    Returns:
        None
    """
    _cleanup_outgoing_context(current())
    _context.reset(token)


@contextmanager
def scoped(
    context: SkyvernContext,
    *,
    propagate_captcha_timeout: bool = False,
) -> Iterator[SkyvernContext]:
    """
    Temporarily scope the current context to a fresh child context.

    Args:
        context: The child context to set for the scope
        propagate_captcha_timeout: When True, copy the child's
            ``consecutive_captcha_timeouts`` back to the parent on exit.
            Only enable for scopes that represent real task executions
            (e.g. run_task_v2), not placeholder contexts.

    Yields:
        The child context
    """
    parent = _context.get() if propagate_captcha_timeout else None
    token = _context.set(context)
    try:
        yield context
    finally:
        if parent is not None:
            parent.consecutive_captcha_timeouts = context.consecutive_captcha_timeouts
        _restore(token)


def reset() -> None:
    """
    Reset the current context

    Returns:
        None
    """
    _cleanup_outgoing_context(current())
    _context.set(None)
