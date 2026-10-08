import asyncio
import time
from dataclasses import dataclass

import structlog

from skyvern.forge.sdk.routes.streaming.channels.exfiltration import ExfiltratedEvent
from skyvern.services.browser_recording.interpretation import (
    OnRecordingInterpretationUpdate,
    RecordingInterpretationSession,
)
from skyvern.services.browser_recording.types import Action, RecordingDraftStep

LOG = structlog.get_logger(__name__)

# Also bounds the draft-inheritance window: an unfinished session abandoned
# without Done/Discard (tab closed, network drop) stays reusable this long.
SESSION_TTL_SECONDS = 60 * 30


@dataclass(frozen=True)
class FinalizedRecordingActions:
    browser_session_id: str
    organization_id: str
    workflow_permanent_id: str
    actions: list[Action]


class RecordingInterpretationSessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, RecordingInterpretationSession] = {}
        self._last_seen: dict[str, float] = {}
        self._finalized_actions: dict[str, FinalizedRecordingActions] = {}
        self._finalized_last_seen: dict[str, float] = {}
        self._stopping_sessions: dict[str, asyncio.Future[list[RecordingDraftStep]]] = {}

    def start_session(
        self,
        *,
        browser_session_id: str,
        organization_id: str,
        workflow_permanent_id: str,
        on_update: OnRecordingInterpretationUpdate,
        deltas_enabled: bool = False,
        recording_attempt_id: str | None = None,
    ) -> None:
        self._prune_expired_sessions()
        existing = self._sessions.get(browser_session_id)
        # Reuse the cached session for any reconnect to the same unfinished
        # recording, even under a different recording_attempt_id. The client mints
        # the id once per recording and keeps it stable across reconnects and
        # stream remounts, so a differing id here means the client lost its
        # in-memory state (e.g. page reload) — wiping would drop every accumulated
        # draft (SKY-12429). Continuing is safe: emit_snapshot resyncs the fresh
        # client, and a completed recording never hits this branch because
        # Done/Discard both end exfiltration, which pops the session. Deliberate
        # trade-off: a recording abandoned WITHOUT Done/Discard reaching the
        # backend stays inheritable for SESSION_TTL_SECONDS, so a new recording on
        # the same browser session + workflow within that window resumes the
        # abandoned drafts (recovery) rather than starting empty; the user can
        # discard them, which now clears this cache.
        if (
            existing is not None
            and existing.workflow_permanent_id == workflow_permanent_id
            and existing.organization_id == organization_id
            and not existing.finalized
        ):
            if recording_attempt_id is not None and existing.recording_attempt_id != recording_attempt_id:
                LOG.info(
                    "Continuing recording interpretation session under a new attempt id",
                    browser_session_id=browser_session_id,
                    previous_recording_attempt_id=existing.recording_attempt_id,
                    recording_attempt_id=recording_attempt_id,
                    accumulated_step_count=len(existing.steps),
                )
                existing.recording_attempt_id = recording_attempt_id
            existing.on_update = on_update
            existing.set_deltas_enabled(deltas_enabled)
            self._last_seen[browser_session_id] = time.monotonic()
            existing.emit_snapshot()
            return

        self.discard_session(browser_session_id)
        self._sessions[browser_session_id] = RecordingInterpretationSession(
            browser_session_id=browser_session_id,
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
            on_update=on_update,
            deltas_enabled=deltas_enabled,
            recording_attempt_id=recording_attempt_id,
        )
        self._last_seen[browser_session_id] = time.monotonic()

    def ingest_events(self, browser_session_id: str, events: list[ExfiltratedEvent]) -> None:
        self._prune_expired_sessions()
        session = self._sessions.get(browser_session_id)
        if not session:
            return

        self._last_seen[browser_session_id] = time.monotonic()
        session.ingest_events(events)

    def pause_capture(self, browser_session_id: str) -> None:
        session = self._sessions.get(browser_session_id)
        if not session:
            return

        session.pause_capture()

    def resume_capture(self, browser_session_id: str) -> None:
        session = self._sessions.get(browser_session_id)
        if not session:
            return

        session.resume_capture()

    async def stop_session(
        self,
        browser_session_id: str,
        *,
        retain_finalized_actions: bool = True,
    ) -> list[RecordingDraftStep]:
        stopping = self._stopping_sessions.get(browser_session_id)
        if stopping is not None:
            return await asyncio.shield(stopping)

        stopping = asyncio.get_running_loop().create_future()
        self._stopping_sessions[browser_session_id] = stopping
        try:
            drafts = await self._stop_session(
                browser_session_id,
                retain_finalized_actions=retain_finalized_actions,
            )
        except asyncio.CancelledError:
            stopping.cancel()
            raise
        except Exception as exc:
            stopping.set_exception(exc)
            # The initiating caller receives the exception directly. Mark the
            # future observed too in case there were no concurrent waiters.
            stopping.exception()
            raise
        else:
            stopping.set_result(drafts)
            return drafts
        finally:
            if self._stopping_sessions.get(browser_session_id) is stopping:
                self._stopping_sessions.pop(browser_session_id, None)

    async def _stop_session(
        self,
        browser_session_id: str,
        *,
        retain_finalized_actions: bool,
    ) -> list[RecordingDraftStep]:
        session = self._sessions.pop(browser_session_id, None)
        self._last_seen.pop(browser_session_id, None)
        if not session:
            return []

        try:
            drafts = await session.flush()
            if retain_finalized_actions:
                self._finalized_actions[session.interpretation_session_id] = FinalizedRecordingActions(
                    browser_session_id=session.browser_session_id,
                    organization_id=session.organization_id,
                    workflow_permanent_id=session.workflow_permanent_id,
                    actions=session.recorded_actions(),
                )
                self._finalized_last_seen[session.interpretation_session_id] = time.monotonic()
            return drafts
        finally:
            session.cancel()

    def get_finalized_actions(
        self,
        *,
        interpretation_session_id: str | None,
        browser_session_id: str,
        organization_id: str,
        workflow_permanent_id: str,
    ) -> list[Action] | None:
        self._prune_expired_sessions()
        if interpretation_session_id is None:
            return None

        finalized = self._finalized_actions.get(interpretation_session_id)
        if finalized is None:
            return None
        if (
            finalized.browser_session_id != browser_session_id
            or finalized.organization_id != organization_id
            or finalized.workflow_permanent_id != workflow_permanent_id
        ):
            LOG.warning(
                "Rejected finalized recording actions with mismatched ownership",
                interpretation_session_id=interpretation_session_id,
                browser_session_id=browser_session_id,
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
            )
            return None

        self._finalized_last_seen[interpretation_session_id] = time.monotonic()
        return list(finalized.actions)

    def get_live_session(
        self,
        *,
        browser_session_id: str,
        organization_id: str,
        workflow_permanent_id: str,
    ) -> RecordingInterpretationSession | None:
        self._prune_expired_sessions()
        session = self._sessions.get(browser_session_id)
        if session is None:
            return None
        if session.organization_id != organization_id or session.workflow_permanent_id != workflow_permanent_id:
            LOG.warning(
                "Rejected live recording session with mismatched ownership",
                browser_session_id=browser_session_id,
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
            )
            return None
        return session

    def discard_finalized_actions(self, interpretation_session_id: str | None) -> None:
        if interpretation_session_id is None:
            return

        self._finalized_actions.pop(interpretation_session_id, None)
        self._finalized_last_seen.pop(interpretation_session_id, None)

    def discard_finalized_actions_if_owned(
        self,
        *,
        interpretation_session_id: str,
        browser_session_id: str,
        organization_id: str,
        workflow_permanent_id: str,
    ) -> bool:
        actions = self.get_finalized_actions(
            interpretation_session_id=interpretation_session_id,
            browser_session_id=browser_session_id,
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
        )
        if actions is None:
            return False
        self.discard_finalized_actions(interpretation_session_id)
        return True

    def discard_session(self, browser_session_id: str) -> None:
        session = self._sessions.pop(browser_session_id, None)
        self._last_seen.pop(browser_session_id, None)
        if session:
            session.cancel()

    def _prune_expired_sessions(self) -> None:
        now = time.monotonic()
        expired_session_ids = [
            browser_session_id
            for browser_session_id, last_seen in self._last_seen.items()
            if now - last_seen > SESSION_TTL_SECONDS
        ]
        for browser_session_id in expired_session_ids:
            LOG.info("Pruning stale recording interpretation session", browser_session_id=browser_session_id)
            self.discard_session(browser_session_id)

        expired_finalized_ids = [
            interpretation_session_id
            for interpretation_session_id, last_seen in self._finalized_last_seen.items()
            if now - last_seen > SESSION_TTL_SECONDS
        ]
        for interpretation_session_id in expired_finalized_ids:
            self._finalized_last_seen.pop(interpretation_session_id, None)
            self._finalized_actions.pop(interpretation_session_id, None)

    async def stop_all(self) -> None:
        await asyncio.gather(*(self.stop_session(browser_session_id) for browser_session_id in list(self._sessions)))


# Process-local singleton. Requires sticky routing (or a single worker) so the message
# WebSocket, final action handoff, and interpretation session stay on the same API instance.
# Multi-pod deployments without affinity need shared session storage (e.g. Redis).
interpretation_registry = RecordingInterpretationSessionRegistry()
