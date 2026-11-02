from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import structlog

from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun
from skyvern.webeye.browser_artifacts import RecordingPrefixSnapshot, VideoArtifact
from skyvern.webeye.browser_state import BrowserState

if TYPE_CHECKING:
    from skyvern.forge.sdk.executor.run_liveness import RunOwner
    from skyvern.forge.sdk.streaming.registries import _ClosingStreamState


@dataclass(frozen=True, eq=False)
class SupersededRunClaim:
    """What a superseded execution detached for one run, with no await, before releasing it."""

    # The superseded execution that claimed; release re-checks it against a successor registered since.
    owner: RunOwner | None = None
    # Already removed from this run's lookup entries; closed only when ``close`` (exclusively owned).
    browser_state: BrowserState | None = None
    close: bool = False
    # The run's pinned engine selection, retired only if it is still this exact object at release.
    engine_owner: object | None = None
    # Exclusive browsers of this execution that a registered successor published over; closed at release.
    displaced_browser_states: tuple[BrowserState, ...] = ()


LOG = structlog.get_logger()


@dataclass(frozen=True)
class BrowserCleanupResult:
    browser_state: BrowserState | None
    recording_finalized: bool


class BrowserManager(Protocol):
    pages: dict[str, BrowserState]

    async def get_or_create_for_task(self, task: Task, browser_session_id: str | None = None) -> BrowserState: ...

    async def get_or_create_for_workflow_run(
        self,
        workflow_run: WorkflowRun,
        url: str | None = None,
        browser_session_id: str | None = None,
        browser_profile_id: str | None = None,
        navigate: bool = True,
        browser_session_runnable_id: str | None = None,
        browser_session_runnable_generation_id: str | None = None,
        before_navigation: Callable[[BrowserState], None] | None = None,
    ) -> BrowserState: ...

    async def cleanup_for_task(
        self,
        task_id: str,
        close_browser_on_completion: bool = True,
        browser_session_id: str | None = None,
        organization_id: str | None = None,
    ) -> BrowserState | None: ...

    async def cleanup_for_workflow_run(
        self,
        workflow_run_id: str,
        task_ids: list[str],
        close_browser_on_completion: bool = True,
        browser_session_id: str | None = None,
        organization_id: str | None = None,
        child_workflow_run_ids: list[str] | None = None,
        stream_tombstone: _ClosingStreamState | None = None,
    ) -> BrowserCleanupResult: ...

    async def get_or_create_for_script(
        self,
        script_id: str | None = None,
        browser_session_id: str | None = None,
        organization_id: str | None = None,
    ) -> BrowserState: ...

    def acquiring_session_runnable(self, runnable_id: str | None) -> AbstractAsyncContextManager[None]: ...

    def live_session_runnable_ids(self) -> set[str]:
        """Runnables holding a persistent session in this process — leased, or mid-acquisition — i.e.
        still running here. An implementation that tracks none reports none and is never protected."""
        return set()

    def claim_superseded_run_resources(self, workflow_run_id: str, owner: RunOwner) -> SupersededRunClaim:
        """Unpublish, with no await, the browser publication and session lease ``owner`` still holds for the run."""
        return SupersededRunClaim()

    async def release_superseded_run_resources(self, workflow_run_id: str, claim: SupersededRunClaim) -> bool:
        """Finish a claim without writing back its session; returns whether a browser close returned."""
        return False

    def inherit_deferred_session_lease(
        self,
        workflow_run_id: str,
        *,
        session_id: str,
        organization_id: str,
        runnable_id: str,
        runnable_generation_id: str | None,
        browser_state: BrowserState,
    ) -> None:
        """Hand a deferred close's persistent-session lease to the run's successor that holds its browser."""
        return None

    def is_held_by_newer_adopter(self, workflow_run_id: str, owner: RunOwner, browser_state: BrowserState) -> bool:
        """Whether a still-registered execution newer than ``owner`` adopted ``browser_state`` and disposes of it."""
        return False

    async def begin_session_activity_renewal(self, session_id: str | None) -> None:
        """Keep a session's ``last_activity_at`` fresh for a caller that holds no cleanup lease.

        Balanced by ``end_session_activity_renewal``. An implementation without a persistent-session
        lifecycle has nothing to renew."""

    def end_session_activity_renewal(self, session_id: str | None) -> None: ...

    async def cleanup_for_script(
        self,
        script_id: str,
        close_browser_on_completion: bool = True,
        browser_session_id: str | None = None,
        organization_id: str | None = None,
    ) -> BrowserState | None: ...

    def evict_page(self, page_id: str) -> None: ...

    def get_for_task(self, task_id: str, workflow_run_id: str | None = None) -> BrowserState | None: ...

    def get_for_workflow_run(
        self,
        workflow_run_id: str,
        parent_workflow_run_id: str | None = None,
    ) -> BrowserState | None: ...

    def get_for_script(self, script_id: str | None = None) -> BrowserState | None: ...

    def set_video_artifact_for_task(self, task: Task, artifacts: list[VideoArtifact]) -> None: ...

    async def get_video_artifacts(
        self,
        browser_state: BrowserState,
        task_id: str = "",
        workflow_id: str = "",
        workflow_run_id: str = "",
        finalize: bool = True,
    ) -> list[VideoArtifact]: ...

    def snapshot_recording_prefixes(
        self,
        browser_state: BrowserState,
        task_id: str = "",
    ) -> list[RecordingPrefixSnapshot] | None: ...

    async def get_har_data(
        self,
        browser_state: BrowserState,
        task_id: str = "",
        workflow_id: str = "",
        workflow_run_id: str = "",
    ) -> bytes: ...

    async def get_browser_console_log(
        self,
        browser_state: BrowserState,
        task_id: str = "",
        workflow_id: str = "",
        workflow_run_id: str = "",
    ) -> bytes: ...
