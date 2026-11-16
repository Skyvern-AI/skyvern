from __future__ import annotations

import asyncio
from _thread import LockType
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

import structlog
from playwright.async_api import Browser, BrowserContext, CDPSession

LOG = structlog.get_logger()


@dataclass(eq=False)
class BrowserResponseAdmission:
    owner: BrowserResponseCapture
    target_id: str
    scope_is_active: Callable[[], bool]
    stream_taken: bool = False
    frame_session: CDPSession | None = None
    detached_frames: set[str] | None = None
    frame_ancestors: frozenset[str] = frozenset()

    def is_active(self) -> bool:
        return (
            self.owner.active
            and self.owner.browser.is_connected()
            and self.scope_is_active()
            and self.target_id not in self.owner.destroyed_targets
            and not self.frame_ancestors.intersection(self.detached_frames or ())
        )

    async def refresh(self) -> bool:
        if self.frame_session is not None:
            try:
                result = await asyncio.wait_for(self.frame_session.send("Page.getFrameTree"), 1)
                pending = [(result.get("frameTree", {}), frozenset[str]())]
                while pending:
                    node, ancestors = pending.pop()
                    ancestors = ancestors | {node.get("frame", {}).get("id", "")}
                    if ancestors == self.frame_ancestors:
                        return self.is_active()
                    pending.extend((child, ancestors) for child in node.get("childFrames", []))
            except Exception:
                pass
            with self.owner.publication_lock:
                self.detached_frames = set(self.frame_ancestors)
            return False
        return self.is_active()

    async def release(self) -> None:
        with self.owner.publication_lock:
            self.owner.admissions.discard(self)
            self.owner._prune_lifetimes()
        if self.frame_session is not None:
            try:
                await asyncio.wait_for(self.frame_session.detach(), 0.25)
            except Exception:
                pass
            self.frame_session = None


class BrowserResponseCapture:
    def __init__(
        self, browser: Browser, context: BrowserContext, session: CDPSession, publication_lock: LockType
    ) -> None:
        self.browser = browser
        self.context = context
        self.session = session
        self.context_id: str | None = None
        self.active = False
        self.publication_lock = publication_lock
        self.identified_frames: dict[str, str] = {}
        self.admissions: set[BrowserResponseAdmission] = set()
        self.resolving = 0
        self.destroyed_targets: set[str] = set()
        self.pending_downloads: dict[str, tuple[str | None, str]] = {}
        self.blocked_downloads: set[tuple[str | None, str]] = set()
        self.started_requests: set[str] = set()
        self.handlers: dict[asyncio.Task[None], str] = {}

    async def enroll(self) -> None:
        if self.context.browser is not self.browser or not self.context.pages:
            raise RuntimeError("Browser response capture requires a context member to establish identity")
        page = self.context.pages[0]
        page_session = await self.context.new_cdp_session(page)
        try:
            info = (await page_session.send("Target.getTargetInfo")).get("targetInfo", {})
            context_id = info.get("browserContextId")
            if not isinstance(context_id, str) or not context_id:
                raise RuntimeError("Browser response capture requires positive context identity")
            self.context_id = context_id
        finally:
            await page_session.detach()
        if page.is_closed() or not self.browser.is_connected():
            raise RuntimeError("Browser response context closed during enrollment")
        self.session.on("Target.targetDestroyed", self._target_destroyed)
        await self.session.send("Target.setDiscoverTargets", {"discover": True})
        if page.is_closed() or not any(context is self.context for context in self.browser.contexts):
            raise RuntimeError("Browser response context closed during enrollment")
        self.active = True

    def _target_destroyed(self, event: dict[str, Any]) -> None:
        target_id = event.get("targetId")
        if isinstance(target_id, str):
            with self.publication_lock:
                if (
                    self.resolving
                    or target_id in self.identified_frames.values()
                    or any(a.target_id == target_id for a in self.admissions)
                ):
                    self.destroyed_targets.add(target_id)
                self._prune_lifetimes()

    def close(self) -> None:
        self.fence_pending_downloads()
        with self.publication_lock:
            self.active = False
        self.session.remove_listener("Target.targetDestroyed", self._target_destroyed)

    def fence_pending_downloads(self) -> None:
        for key in tuple(self.pending_downloads.values()):
            self.fence_download(key)

    def fence_download(self, key: tuple[str | None, str]) -> None:
        with self.publication_lock:
            key = key if key[0] in self.identified_frames else (None, key[1])
            if key not in self.blocked_downloads and len(self.blocked_downloads) >= 256:
                self.active = False
                LOG.error("Browser response capture denial capacity exhausted")
                return
            self.blocked_downloads.add(key)

    def _identify_frame(self, frame_id: str, target_id: str) -> None:
        with self.publication_lock:
            self.identified_frames[frame_id] = target_id

    def _prune_lifetimes(self) -> None:
        if self.resolving:
            return
        live = {a.target_id for a in self.admissions}
        expired = {f for f, t in self.identified_frames.items() if t in self.destroyed_targets and t not in live}
        self.blocked_downloads.difference_update(k for k in tuple(self.blocked_downloads) if k[0] in expired)
        retained = {frame for frame, _ in (*self.pending_downloads.values(), *self.blocked_downloads)}
        self.identified_frames = {f: t for f, t in self.identified_frames.items() if t in live or f in retained}
        self.destroyed_targets.intersection_update(live)

    @staticmethod
    def download_key(event: dict[str, Any]) -> tuple[str | None, str]:
        frame_id = event.get("frameId")
        return (
            frame_id if isinstance(frame_id, str) else None,
            event.get("request", {}).get("url", event.get("url", "")),
        )

    def blocks_recovery(self, event: dict[str, Any]) -> bool:
        frame_id, url = self.download_key(event)
        return (
            (frame_id, url) in self.blocked_downloads
            or (None, url) in self.blocked_downloads
            or any(blocked_url == url and frame_id is None for _, blocked_url in self.blocked_downloads)
            or any(
                pending_url == url and (pending_frame == frame_id or pending_frame not in self.identified_frames)
                for pending_frame, pending_url in self.pending_downloads.values()
            )
        )

    def _owns(self, info: dict[str, Any], target_id: str) -> bool:
        return (
            self.active
            and self.browser.is_connected()
            and any(context is self.context for context in self.browser.contexts)
            and bool(self.context_id)
            and info.get("browserContextId") == self.context_id
            and info.get("targetId") == target_id
            and info.get("type") in {"page", "iframe"}
            and target_id not in self.destroyed_targets
        )

    async def admit(self, frame_id: object, scope_is_active: Callable[[], bool]) -> BrowserResponseAdmission | None:
        if not isinstance(frame_id, str) or not frame_id or not self.active or not scope_is_active():
            return None
        admission = None
        accepted = False
        self.resolving += 1
        try:
            async with asyncio.timeout(1):
                admission = await self._resolve_frame(frame_id, scope_is_active)
                if admission is None or not admission.is_active():
                    return None
                self.admissions.add(admission)
                target_id = admission.target_id
                info = (await self.session.send("Target.getTargetInfo", {"targetId": target_id})).get("targetInfo", {})
                if not self._owns(info, target_id) or not admission.is_active():
                    return None
                self._identify_frame(frame_id, target_id)
                accepted = True
                return admission
        except Exception:
            return None
        finally:
            self.resolving -= 1
            if admission is not None and not accepted:
                await admission.release()

    async def _resolve_frame(
        self, frame_id: str, scope_is_active: Callable[[], bool]
    ) -> BrowserResponseAdmission | None:
        # A frame is not necessarily a target. Only a successful, context-bound target lookup can
        # admit this fast path; subframes require their actual owner's frame tree below.
        try:
            info = (await self.session.send("Target.getTargetInfo", {"targetId": frame_id})).get("targetInfo", {})
            if (
                info.get("targetId") == frame_id
                and info.get("type") in {"page", "iframe"}
                and info.get("browserContextId")
            ):
                self._identify_frame(frame_id, frame_id)
            if self._owns(info, frame_id):
                return BrowserResponseAdmission(self, frame_id, scope_is_active)
        except Exception:
            pass
        for page in tuple(self.context.pages):
            if page.is_closed():
                continue
            page_session = await self.context.new_cdp_session(page)
            retained = False
            detached: set[str] = set()
            page_session.on("Page.frameDetached", partial(self._frame_detached, detached))
            try:
                await page_session.send("Page.enable")
                info = (await page_session.send("Target.getTargetInfo")).get("targetInfo", {})
                target_id = info.get("targetId")
                if not isinstance(target_id, str) or not self._owns(info, target_id):
                    continue
                tree = (await page_session.send("Page.getFrameTree")).get("frameTree", {})
                pending = [(tree, frozenset[str]())]
                while pending:
                    node, ancestors = pending.pop()
                    node_id = node.get("frame", {}).get("id", "")
                    ancestors = ancestors | {node_id}
                    if node_id == frame_id:
                        retained = True
                        return BrowserResponseAdmission(
                            self,
                            target_id,
                            scope_is_active,
                            frame_session=page_session,
                            detached_frames=detached,
                            frame_ancestors=ancestors,
                        )
                    pending.extend((child, ancestors) for child in node.get("childFrames", []))
            finally:
                if not retained:
                    try:
                        await asyncio.wait_for(page_session.detach(), 0.25)
                    except Exception:
                        pass
        return None

    def _frame_detached(self, detached: set[str], event: dict[str, Any]) -> None:
        with self.publication_lock:
            detached.add(event.get("frameId", ""))
