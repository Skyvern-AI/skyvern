from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

import structlog

from skyvern.services.browser_recording.v2.keyfold import Fact, fold
from skyvern.services.browser_recording.v2.ledger import Effect, Gesture, start_ledger, stop_ledger
from skyvern.services.browser_recording.v2.observation import start_observer
from skyvern.services.browser_recording.v2.resolver import start_resolver

LOG = structlog.get_logger()

INTERPRET_INTERVAL_SECONDS = 0.15
MAX_TICK_FAILURES = 5

StepKind = Literal["click", "type_text", "press_key", "goto_url", "download", "upload", "dialog"]

_EFFECT_STEPS: dict[str, tuple[StepKind, str]] = {
    "download": ("download", "Download file"),
    "file_chooser": ("upload", "Upload file"),
    "dialog": ("dialog", "Handle dialog"),
}


@dataclass(slots=True)
class StepV2:
    step_id: str
    kind: StepKind
    title: str
    t_start: float
    t_end: float
    url: str | None
    selector: str | None
    role: str | None
    accessible_name: str | None
    frame_id: str | None
    typed_length: int = 0
    settle_ms: int | None = None
    gesture_seqs: list[int] = field(default_factory=list)


@dataclass(slots=True)
class RecordingUpdateV2:
    session_revision: int
    steps: list[StepV2]
    changed_steps: list[StepV2]
    is_snapshot: bool
    pending: bool
    finalized: bool
    # StepV2 timestamps are monotonic; the wire wants epoch ms.
    epoch_offset_ms: float = 0.0


def _target_name(fact: Fact) -> str:
    name = fact.accessible_name or (fact.tag.lower() if fact.tag else "")
    return name.strip() or "element"


def _short_url(url: str | None) -> str:
    if not url:
        return "page"
    parsed = urlparse(url)
    if not parsed.netloc:
        return url
    path = "" if parsed.path in ("", "/") else parsed.path
    return f"{parsed.netloc}{path}"


def _fact_title(fact: Fact) -> str:
    if fact.kind == "click":
        return f"Click {_target_name(fact)}"
    if fact.kind == "type_text":
        return f"Type into {_target_name(fact)}"
    return f"Press {fact.key or 'key'}"


def interpret_steps(browser_session_id: str, gestures: Sequence[Gesture], effects: Sequence[Effect]) -> list[StepV2]:
    steps: dict[int, StepV2] = {}
    by_gesture: dict[int, StepV2] = {}

    for fact in fold(gestures):
        first_seq = fact.gesture_seqs[0]
        step = StepV2(
            step_id=f"{browser_session_id}:{first_seq}",
            kind=fact.kind,
            title=_fact_title(fact),
            t_start=fact.t_start,
            t_end=fact.t_end,
            url=fact.url or None,
            selector=fact.selector,
            role=fact.role,
            accessible_name=fact.accessible_name,
            frame_id=fact.frame_id,
            typed_length=fact.typed_length,
            gesture_seqs=list(fact.gesture_seqs),
        )
        steps[first_seq] = step
        for gesture_seq in fact.gesture_seqs:
            by_gesture[gesture_seq] = step

    for effect in effects:
        caused = by_gesture.get(effect.caused_by_seq) if effect.caused_by_seq is not None else None
        if effect.kind == "navigation":
            if caused is None and effect.is_main_frame:
                steps[effect.seq] = StepV2(
                    step_id=f"{browser_session_id}:{effect.seq}",
                    kind="goto_url",
                    title=f"Go to {_short_url(effect.url)}",
                    t_start=effect.t_received,
                    t_end=effect.t_received,
                    url=effect.url,
                    selector=None,
                    role=None,
                    accessible_name=None,
                    frame_id=effect.frame_id,
                )
        elif effect.kind == "network_settle":
            if caused is not None and effect.busy_ms is not None:
                caused.settle_ms = effect.busy_ms
        elif (effect_step := _EFFECT_STEPS.get(effect.kind)) is not None:
            effect_kind, effect_title = effect_step
            steps[effect.seq] = StepV2(
                step_id=f"{browser_session_id}:{effect.seq}",
                kind=effect_kind,
                title=effect_title,
                t_start=effect.t_received,
                t_end=effect.t_received,
                url=effect.url,
                selector=None,
                role=None,
                accessible_name=None,
                frame_id=effect.frame_id,
            )

    return [steps[key] for key in sorted(steps)]


class RecordingSessionV2:
    def __init__(
        self,
        *,
        browser_session_id: str,
        organization_id: str,
        workflow_permanent_id: str | None,
        on_update: Callable[[RecordingUpdateV2], None],
    ) -> None:
        self.browser_session_id = browser_session_id
        self.organization_id = organization_id
        self.workflow_permanent_id = workflow_permanent_id
        self.on_update = on_update
        self.ledger = start_ledger(browser_session_id)
        self.resolver = start_resolver(browser_session_id, self.ledger)
        self.observer = start_observer(browser_session_id, self.ledger)
        self.sealed = False
        self.epoch_offset_ms = time.time() * 1000 - time.monotonic() * 1000
        self._steps: list[StepV2] = []
        self._emitted: dict[str, StepV2] = {}
        self._revision = 0
        self._interpreted_version = 0
        self._ticker: asyncio.Task[None] | None = None

    @property
    def steps(self) -> list[StepV2]:
        return list(self._steps)

    async def attach_page(self, page_key: str, cdp_session: Any) -> None:
        await self.observer.attach_page_session(page_key, cdp_session)

    async def attach_browser(self, cdp_session: Any) -> None:
        await self.observer.attach_browser_session(cdp_session)

    def pause(self) -> None:
        self.ledger.paused = True

    def resume(self) -> None:
        self.ledger.paused = False

    def interpret(self) -> None:
        self._emit(is_snapshot=False)

    async def seal(self) -> list[StepV2]:
        self.sealed = True
        self.ledger.paused = True
        await self.stop_ticker()
        self._emit(is_snapshot=True)
        return self.steps

    def discard(self) -> None:
        self.sealed = True
        self.ledger.paused = True
        if self._ticker is not None:
            self._ticker.cancel()
            self._ticker = None
        stop_ledger(self.browser_session_id)

    def resume_streaming(self, on_update: Callable[[RecordingUpdateV2], None]) -> None:
        self.on_update = on_update
        self._start_ticker()
        self._emit(is_snapshot=True)

    def _emit(self, *, is_snapshot: bool) -> None:
        self._interpreted_version = self.ledger.version
        steps = interpret_steps(self.browser_session_id, self.ledger.rows(), self.ledger.effects())
        changed = [step for step in steps if self._emitted.get(step.step_id) != step]
        removed = self._emitted.keys() - {step.step_id for step in steps}
        snapshot = is_snapshot or self._revision == 0 or bool(removed)
        if not changed and not snapshot:
            return

        self._steps = steps
        self._emitted = {step.step_id: step for step in steps}
        self._revision += 1
        self.on_update(
            RecordingUpdateV2(
                session_revision=self._revision,
                steps=steps,
                changed_steps=[] if snapshot else changed,
                is_snapshot=snapshot,
                pending=not self.sealed,
                finalized=self.sealed,
                epoch_offset_ms=self.epoch_offset_ms,
            )
        )

    def _start_ticker(self) -> None:
        if self.sealed or (self._ticker is not None and not self._ticker.done()):
            return
        self._ticker = asyncio.create_task(self._tick())

    async def stop_ticker(self) -> None:
        ticker, self._ticker = self._ticker, None
        if ticker is None:
            return
        ticker.cancel()
        try:
            await ticker
        except asyncio.CancelledError:
            pass

    async def _tick(self) -> None:
        failures = 0
        while not self.sealed:
            await asyncio.sleep(INTERPRET_INTERVAL_SECONDS)
            if self._interpreted_version == self.ledger.version:
                continue
            try:
                self.interpret()
            except Exception:
                failures += 1
                if failures >= MAX_TICK_FAILURES:
                    LOG.warning(
                        "Record Browser v2 interpretation failed repeatedly; stopping live updates",
                        browser_session_id=self.browser_session_id,
                        exc_info=True,
                    )
                    return
            else:
                failures = 0


sessions_v2: dict[str, RecordingSessionV2] = {}
# A socket that drops without sealing keeps its session alive on purpose so a reconnect can
# resume it; a client that never comes back would otherwise pin its ledger for the life of the
# pod. The cap is the only thing that reaches stop_ledger for those.
MAX_LIVE_SESSIONS = 64


def _evict_oldest_session() -> None:
    while len(sessions_v2) >= MAX_LIVE_SESSIONS:
        oldest = next(iter(sessions_v2))
        LOG.warning("Dropping the oldest v2 recording session to stay under the cap", browser_session_id=oldest)
        discard_session_v2(oldest)


def start_session_v2(
    *,
    browser_session_id: str,
    organization_id: str,
    workflow_permanent_id: str | None,
    on_update: Callable[[RecordingUpdateV2], None],
) -> RecordingSessionV2:
    session = sessions_v2.get(browser_session_id)
    if session is not None:
        session.resume_streaming(on_update)
        return session

    _evict_oldest_session()
    # A ledger can outlive the session that owned it (a teardown that never sealed).
    stop_ledger(browser_session_id)
    session = RecordingSessionV2(
        browser_session_id=browser_session_id,
        organization_id=organization_id,
        workflow_permanent_id=workflow_permanent_id,
        on_update=on_update,
    )
    sessions_v2[browser_session_id] = session
    session.resume_streaming(on_update)
    return session


def get_session_v2(browser_session_id: str) -> RecordingSessionV2 | None:
    return sessions_v2.get(browser_session_id)


async def stop_session_v2(browser_session_id: str) -> list[StepV2]:
    session = sessions_v2.pop(browser_session_id, None)
    if session is None:
        return []
    steps = await session.seal()
    # The sealed session keeps its ledger for the commit path, but nothing may keep
    # observing: stop_ledger detaches the observer and closes the resolver.
    stop_ledger(browser_session_id)
    return steps


def discard_session_v2(browser_session_id: str) -> None:
    session = sessions_v2.pop(browser_session_id, None)
    if session is not None:
        session.discard()
