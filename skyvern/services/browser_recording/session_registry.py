import asyncio
import time

import structlog

from skyvern.forge.sdk.routes.streaming.channels.exfiltration import ExfiltratedEvent
from skyvern.services.browser_recording.interpretation import (
    OnRecordingInterpretationUpdate,
    RecordingInterpretationSession,
)
from skyvern.services.browser_recording.types import RecordingDraftStep

LOG = structlog.get_logger(__name__)

SESSION_TTL_SECONDS = 60 * 30


class RecordingInterpretationSessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, RecordingInterpretationSession] = {}
        self._last_seen: dict[str, float] = {}

    def start_session(
        self,
        *,
        browser_session_id: str,
        organization_id: str,
        workflow_permanent_id: str,
        on_update: OnRecordingInterpretationUpdate,
        deltas_enabled: bool = False,
    ) -> None:
        self._prune_expired_sessions()
        existing = self._sessions.get(browser_session_id)
        if (
            existing is not None
            and existing.workflow_permanent_id == workflow_permanent_id
            and existing.organization_id == organization_id
            and not existing.finalized
        ):
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

    async def stop_session(self, browser_session_id: str) -> list[RecordingDraftStep]:
        session = self._sessions.pop(browser_session_id, None)
        self._last_seen.pop(browser_session_id, None)
        if not session:
            return []

        try:
            return await session.flush()
        finally:
            session.cancel()

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

    async def stop_all(self) -> None:
        await asyncio.gather(*(self.stop_session(browser_session_id) for browser_session_id in list(self._sessions)))


# Process-local singleton. Requires sticky routing (or a single worker) so the message
# WebSocket and interpretation session stay on the same API instance. Multi-pod deployments
# without affinity need shared session storage (e.g. Redis).
interpretation_registry = RecordingInterpretationSessionRegistry()
