"""Shadow-mode tripwire observability for the agent loop's fail-fast feature (Phase 0).

Detects when a fail-fast "tripwire" WOULD fire on a stuck/looping run and emits a
structured log event the first time each (task, tripwire) trips while the task's ledger
is resident — without ever terminating anything. Pure observability: a log-based metric
joins these `would_fire` events to each task's final status to derive every tripwire's
decision precision (the false-positive rate among its own would-be terminations) BEFORE
any tripwire is allowed to act.

The recorder runs for terminal steps where ``agent_step`` returns a status (completed or
failed, including retries) — where the grinding-loop signals are observable. Steps that
terminate by exception (scrape / browser failures, unsupported actions) carry no page or
action state for these tripwires and already fail fast, so they are out of scope.

Conventions mirror ``extraction_shadow.py``: best-effort (never raises into the loop),
and only hashes / booleans / counts are logged — never raw page or model content.

Gating is OFF by default: the per-org PostHog flag ``FAIL_FAST_SHADOW`` (or the static
``settings.FAIL_FAST_SHADOW`` kill-switch) must opt a run in. State is a bounded,
append-only per-task ledger written only from the authoritative step path, so parallel
verification / speculative planning can never contaminate it.

Limitations — ledger loss censors events NON-randomly, so it can bias the precision
estimate in EITHER direction (not provably one-directional). Expected small because a
task's step loop is in-process and eviction targets idle tasks, but Phase-0 analysis must
quantify the ledger-loss rate (tasks that reached a terminal step with no resident ledger)
before trusting the precision numbers:
  - The ledger is in-memory and per-process. A task's step loop runs in one process, but
    a task that resumes on a different worker starts with an empty ledger.
  - Tripwires are evaluated at fixed default thresholds (see ``_K_*``/``_M_*``/``_N_*``),
    which measures decision precision AT the threshold a first cut would ship. Measuring
    precision across a range of thresholds (to tune them) is a separate follow-up.
  - In-app per-(task, tripwire) dedup is best-effort: it lives in the resident ledger and
    is dropped on LRU eviction, so a re-seen evicted task can re-emit. The offline metric
    is the authoritative dedup (first event per task_id + tripwire_id).
"""

from __future__ import annotations

import secrets
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

from skyvern.config import settings
from skyvern.exceptions import IllegitComplete
from skyvern.forge import app
from skyvern.forge.sdk.api.crypto import calculate_sha256
from skyvern.forge.sdk.cache.extraction_cache import compute_cache_key
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import Action
from skyvern.webeye.scraper.scraped_page import ScrapedPage

LOG = structlog.get_logger()

_SHADOW_EVENT = "fail_fast.shadow_tripwire"
_FLAG = "FAIL_FAST_SHADOW"

# Tripwire ids (kept stable — they key the offline decision-precision metric).
_T_NO_PROGRESS = "no_progress"
_T_ACTION_REPETITION = "action_repetition"
_T_ILLEGIT_STREAK = "illegit_complete_streak"
_T_PLAN_STAGNATION = "plan_stagnation"

# Shadow thresholds. Deliberately conservative; the real per-org values are tuned later
# against the shadow data. These only decide when to LOG a would-fire event.
_K_NO_PROGRESS = 3
_M_ACTION_REPEAT = 3
_N_ILLEGIT_STREAK = 3
_K_PLAN_STAGNATION = 3

_LEDGER_MAXLEN = 24  # per-task ring buffer of recent step fingerprints
_MAX_TASKS = 2000  # bound module memory across concurrent tasks
# Skip the content hash on pathologically large element trees so a huge DOM can't add
# meaningful CPU to a terminal step (no-progress just under-counts there — conservative).
_MAX_HTML_FOR_HASH = 500_000
# Per-process random salt for the in-memory value signatures (never logged). Salting makes
# them non-reversible against dictionary attacks on low-entropy inputs (OTPs, ids).
_VALUE_SALT = secrets.token_hex(16)


class _LoggerLike(Protocol):
    def info(self, event: str, **kwargs: Any) -> None: ...

    def warning(self, event: str, **kwargs: Any) -> None: ...


@dataclass
class _StepFingerprint:
    step_order: int
    retry_index: int
    status: str
    state_fp: str | None  # canonical url+content hash; None when unavailable
    act_fps: tuple[str, ...]  # per-action stable signatures (element-targeting actions only)
    plan_fp: str | None  # hash of concatenated action reasoning; None when empty
    illegit_complete: bool


@dataclass
class _TaskLedger:
    steps: deque[_StepFingerprint] = field(default_factory=lambda: deque(maxlen=_LEDGER_MAXLEN))
    fired: set[str] = field(default_factory=set)  # tripwire ids already logged for this task


_LEDGERS: OrderedDict[str, _TaskLedger] = OrderedDict()


def _value_signature(action: Action) -> str:
    """Salted, non-reversible signature of an action's input value.

    Used ONLY as an in-memory ledger key (never logged) to distinguish re-filling a field
    with the same value from a different value; the salt defeats dictionary attacks.
    """
    raw = action.text or ""
    if action.option is not None:
        raw = f"{raw}|{action.option}"
    return calculate_sha256(f"{_VALUE_SALT}{raw}")[:12] if raw else ""


def _act_fp(action: Action) -> str:
    element = action.skyvern_element_hash or action.element_id or ""
    return f"{action.action_type}:{element}:{_value_signature(action)}"


def _state_fp(scraped_page: ScrapedPage | None) -> str | None:
    """Stable content+url hash that ignores transient ids/nonces, or None if unavailable."""
    if scraped_page is None:
        return None
    html = scraped_page.last_used_element_tree_html
    if not html or len(html) > _MAX_HTML_FOR_HASH:
        return None
    try:
        return compute_cache_key(call_path="fail_fast_shadow", element_tree=html, current_url=scraped_page.url)
    except Exception:  # noqa: BLE001 — a bad page must not lose the other signals
        return None


def _build_fingerprint(step: Step, scraped_page: ScrapedPage | None) -> _StepFingerprint:
    actions: list[Action] = []
    illegit = False
    output = step.output
    if output is not None and output.actions_and_results:
        for action, results in output.actions_and_results:
            actions.append(action)
            if action.action_type == ActionType.COMPLETE:
                for result in results:
                    if result.exception_type == IllegitComplete.__name__:
                        illegit = True
                        break

    # Only true element interactions: a non-web action (WAIT/COMPLETE/TERMINATE) can carry a
    # hallucinated skyvern_element_hash (parse_actions clears element_id but not the hash), which
    # would otherwise let repeated waits/completes trip action_repetition. Mirror parse_actions.py.
    act_fps = tuple(
        _act_fp(a)
        for a in actions
        if (a.action_type.is_web_action() or a.action_type == ActionType.SCROLL)
        and (a.skyvern_element_hash or a.element_id)
    )
    reasoning = " ".join(a.reasoning or "" for a in actions).strip()
    plan_fp = calculate_sha256(reasoning)[:16] if reasoning else None

    return _StepFingerprint(
        step_order=step.order,
        retry_index=step.retry_index,
        status=str(step.status),
        state_fp=_state_fp(scraped_page),
        act_fps=act_fps,
        plan_fp=plan_fp,
        illegit_complete=illegit,
    )


def _trailing_run(values: list[str | None]) -> int:
    """Length of the trailing run of equal, non-None values (from the end)."""
    run = 0
    anchor: str | None = None
    for value in reversed(values):
        if value is None:
            break
        if anchor is None:
            anchor = value
            run = 1
        elif value == anchor:
            run += 1
        else:
            break
    return run


def _evaluate(steps: deque[_StepFingerprint]) -> list[tuple[str, dict[str, Any]]]:
    """Tripwires that WOULD fire given the ledger. Pure — no IO, no mutation."""
    seq = list(steps)
    fired: list[tuple[str, dict[str, Any]]] = []
    if not seq:
        return fired

    # T0.1a no-progress: page content unchanged for K consecutive steps.
    state_run = _trailing_run([fp.state_fp for fp in seq])
    if state_run >= _K_NO_PROGRESS:
        fired.append((_T_NO_PROGRESS, {"streak": state_run}))

    # T0.1b action repetition: the same element interaction recurs across M+ steps.
    step_counts: Counter[str] = Counter()
    for fp in seq:
        for act in set(fp.act_fps):
            step_counts[act] += 1
    if step_counts:
        _, top_count = step_counts.most_common(1)[0]
        if top_count >= _M_ACTION_REPEAT:
            fired.append((_T_ACTION_REPETITION, {"repeats": top_count}))

    # T0.2 illegit-complete streak in the same stuck state. Break on an unknown (None) state so
    # we never count a streak we can't confirm is the same state — consistent with _trailing_run.
    # `anchored` distinguishes "not yet anchored" from a real state_fp that happens to be None.
    illegit_run = 0
    anchor_state: str | None = None
    anchored = False
    for fp in reversed(seq):
        if not fp.illegit_complete or fp.state_fp is None:
            break
        if not anchored:
            anchor_state = fp.state_fp
            anchored = True
            illegit_run = 1
        elif fp.state_fp == anchor_state:
            illegit_run += 1
        else:
            break
    if illegit_run >= _N_ILLEGIT_STREAK:
        fired.append((_T_ILLEGIT_STREAK, {"streak": illegit_run}))

    # T0.3 plan/reasoning stagnation (corroborating signal).
    plan_run = _trailing_run([fp.plan_fp for fp in seq])
    if plan_run >= _K_PLAN_STAGNATION:
        fired.append((_T_PLAN_STAGNATION, {"streak": plan_run}))

    return fired


def _get_ledger(task_id: str) -> _TaskLedger:
    ledger = _LEDGERS.get(task_id)
    if ledger is None:
        ledger = _TaskLedger()
        _LEDGERS[task_id] = ledger
        while len(_LEDGERS) > _MAX_TASKS:
            _LEDGERS.popitem(last=False)  # evict the oldest task
    else:
        _LEDGERS.move_to_end(task_id)
    return ledger


def _record_step(ledger: _TaskLedger, fingerprint: _StepFingerprint) -> list[tuple[str, dict[str, Any]]]:
    """Append the fingerprint and return newly-fired (deduped) tripwires. Pure given the ledger."""
    ledger.steps.append(fingerprint)
    newly_fired: list[tuple[str, dict[str, Any]]] = []
    for tripwire_id, signal in _evaluate(ledger.steps):
        if tripwire_id in ledger.fired:
            continue
        ledger.fired.add(tripwire_id)
        newly_fired.append((tripwire_id, signal))
    return newly_fired


async def _shadow_enabled(task: Task, organization: Organization) -> bool:
    if settings.FAIL_FAST_SHADOW:
        return True
    try:
        raw = await app.EXPERIMENTATION_PROVIDER.get_value_cached(
            _FLAG,
            task.workflow_run_id or task.task_id,
            properties={"organization_id": organization.organization_id},
        )
    except Exception:  # noqa: BLE001 — a flag-read failure must never affect the loop
        return False
    if raw is None:
        return False
    if isinstance(raw, bool):
        # PostHog returns a bare bool for a disabled/enabled multivariate flag.
        return raw
    if not isinstance(raw, str):
        return False
    normalized = raw.strip().lower()
    if normalized in ("1", "true", "on", "enabled"):
        return True
    try:
        return float(normalized) > 0
    except (TypeError, ValueError):
        return False


async def record_fail_fast_shadow(
    *,
    task: Task,
    step: Step,
    organization: Organization,
    scraped_page: ScrapedPage | None,
    logger: _LoggerLike | None = None,
) -> None:
    """Evaluate fail-fast tripwires in shadow and emit one ``would_fire`` event per (task, tripwire).

    Never raises and never changes control flow. Call once per terminal step from the
    authoritative step path (after ``agent_step`` returns), for both completed and failed steps.
    """
    log = logger or LOG
    try:
        if step.is_speculative:
            # Speculative steps are discarded; never let them into the authoritative ledger.
            return
        if not await _shadow_enabled(task, organization):
            return
        ledger = _get_ledger(task.task_id)
        fingerprint = _build_fingerprint(step, scraped_page)
        for tripwire_id, signal in _record_step(ledger, fingerprint):
            log.info(
                _SHADOW_EVENT,
                status="would_fire",
                tripwire_id=tripwire_id,
                would_action="terminate",
                task_id=task.task_id,
                workflow_run_id=task.workflow_run_id,
                organization_id=organization.organization_id,
                step_id=step.step_id,
                step_order=step.order,
                retry_index=step.retry_index,
                step_status=str(step.status),
                **{f"signal_{key}": value for key, value in signal.items()},
            )
    except Exception as exc:  # noqa: BLE001 — shadow is best-effort; never affect the agent loop
        log.warning(_SHADOW_EVENT, status="error", error_type=type(exc).__name__, task_id=task.task_id)
