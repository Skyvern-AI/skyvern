from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from hashlib import sha256
from typing import NamedTuple

import structlog

from skyvern.forge.sdk.artifact.models import Artifact

LOG = structlog.get_logger()


@dataclass
class RequestReads:
    """Run data one API request read or handed out links to, for the audit hook."""

    # Owner type and ID, artifact type and optional artifact ID, followed by whether a link was issued.
    artifacts: list[tuple[str, str, str, str | None, bool]] = field(default_factory=list)


class ArtifactLink(NamedTuple):
    resource_type: str
    resource_id: str
    artifact_type: str
    artifact_id: str | None


@dataclass(frozen=True)
class RunStatusLinks:
    """Selected link fingerprints and artifact ownership, with no actor or bearer URL."""

    organization_id: str
    artifacts_by_url_digest: Mapping[bytes, ArtifactLink]


@dataclass
class RunStatusLinkCapture:
    links: RunStatusLinks | None = None


run_status_link_capture: ContextVar[RunStatusLinkCapture | None] = ContextVar("run_status_link_capture", default=None)


@contextmanager
def capture_run_status_links() -> Iterator[RunStatusLinkCapture]:
    capture = RunStatusLinkCapture()
    token = run_status_link_capture.set(capture)
    try:
        yield capture
    finally:
        run_status_link_capture.reset(token)


# Holds one mutable collector per request, so code that runs in a copied context still adds to it.
request_reads: ContextVar[RequestReads | None] = ContextVar("request_reads", default=None)
_defer_file_link_issuance: ContextVar[bool | dict[str, ArtifactLink]] = ContextVar(
    "defer_file_link_issuance", default=False
)


@contextmanager
def defer_file_link_issuance(links_by_url: dict[str, ArtifactLink] | None = None) -> Iterator[None]:
    """Defer issuance until the response selects links, retaining provenance when needed."""
    token = _defer_file_link_issuance.set(True if links_by_url is None else links_by_url)
    try:
        yield
    finally:
        _defer_file_link_issuance.reset(token)


def _artifact_owner(artifact: Artifact) -> tuple[str, str]:
    if artifact.workflow_run_id:
        return "workflow_run", artifact.workflow_run_id
    if artifact.task_id:
        return "task", artifact.task_id
    if artifact.run_id:
        return ("workflow_run" if artifact.run_id.startswith("wr_") else "task"), artifact.run_id
    if artifact.browser_session_id:
        return "browser_session", artifact.browser_session_id
    return "artifact", artifact.artifact_id


def note_read(
    resource_type: str,
    resource_id: str,
    artifact_type: str,
    *,
    link_issued: bool = False,
    artifact_id: str | None = None,
) -> None:
    reads = request_reads.get()
    if reads is not None:
        reads.artifacts.append((resource_type, resource_id, artifact_type, artifact_id, link_issued))


def note_returned_file_links(
    resource_type: str,
    resource_id: str,
    artifact_type: str,
    links: Iterable[tuple[str, str | None]],
) -> None:
    """Record each distinct returned URL, or retain its provenance for a deferred response."""
    deferred = _defer_file_link_issuance.get()
    if deferred is True or (deferred is False and request_reads.get() is None):
        return
    seen_urls: set[str] = set()
    for url, artifact_id in links:
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        if deferred is False:
            note_read(resource_type, resource_id, artifact_type, link_issued=True, artifact_id=artifact_id)
        elif artifact_id or url not in deferred:
            deferred[url] = ArtifactLink(resource_type, resource_id, artifact_type, artifact_id)


def note_artifact_reads(artifacts: Iterable[Artifact], *, link_issued: bool = False) -> None:
    reads = request_reads.get()
    if reads is None or (link_issued and _defer_file_link_issuance.get() is not False):
        return
    # A note never fails the read it describes.
    try:
        for artifact in artifacts:
            owner_type, owner_id = _artifact_owner(artifact)
            reads.artifacts.append(
                (owner_type, owner_id, str(artifact.artifact_type), artifact.artifact_id, link_issued)
            )
    except Exception:
        LOG.warning("Failed to note artifact reads", exc_info=True)


def note_shared_artifact_links(links: Iterable[tuple[Artifact, str | None]]) -> None:
    """Associate deferred links locally, or record issuance through the existing collector."""
    deferred = _defer_file_link_issuance.get()
    if isinstance(deferred, dict):
        try:
            for artifact, url in links:
                if url:
                    deferred[url] = ArtifactLink(
                        *_artifact_owner(artifact), str(artifact.artifact_type), artifact.artifact_id
                    )
        except Exception:
            LOG.warning("Failed to note shared artifact provenance")
    elif not deferred:
        note_artifact_reads((artifact for artifact, url in links if url), link_issued=True)


def artifact_link_digest(url: str) -> bytes:
    return sha256(url.encode(errors="surrogatepass")).digest()


def note_run_status_links(
    links: RunStatusLinks | None, organization_id: str | None, urls: Iterable[str | None]
) -> None:
    """Observe only proven links still present in this caller's response."""
    if links is None or organization_id != links.organization_id or request_reads.get() is None:
        return

    seen: set[bytes] = set()
    for url in urls:
        if not url:
            continue
        digest = artifact_link_digest(url)
        attribution = links.artifacts_by_url_digest.get(digest)
        if attribution is None or digest in seen:
            continue
        resource_type, resource_id, artifact_type, artifact_id = attribution
        note_read(resource_type, resource_id, artifact_type, link_issued=True, artifact_id=artifact_id)
        seen.add(digest)
