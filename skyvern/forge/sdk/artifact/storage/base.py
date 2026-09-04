from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import BinaryIO, cast

from skyvern.forge import app
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType, LogEntityType
from skyvern.forge.sdk.artifact.signing import SENSITIVE_ARTIFACT_TYPES, SENSITIVE_ARTIFACT_URL_EXPIRY_SECONDS
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.ai_suggestions import AISuggestion
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.task_v2 import TaskV2, Thought
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock

# Hour-granularity equivalent of the sensitive cap for the GCS/Azure signing
# clients, whose APIs take hours; max() keeps a future sub-hour cap from
# truncating to zero.
SENSITIVE_SHARE_URL_EXPIRY_HOURS = max(1, SENSITIVE_ARTIFACT_URL_EXPIRY_SECONDS // 3600)


def key_is_org_scoped(key: str, allowed_prefixes: tuple[str, ...]) -> bool:
    """Whether an object key sits under one of the caller's org-scoped prefixes.

    A ``..`` segment is rejected rather than resolved: a bare prefix match on an
    unnormalized key accepts ``{env}/{org_a}/../{org_b}/secret.pdf`` for org_a, and no key
    this service writes ever contains one.
    """
    if any(segment == ".." for segment in key.split("/")):
        return False
    return any(key.startswith(prefix) for prefix in allowed_prefixes)


async def presign_with_sensitive_cap(
    artifacts: list[Artifact],
    presign: Callable[[list[str]], Awaitable[list[str] | None]],
    presign_sensitive: Callable[[list[str]], Awaitable[list[str] | None]],
) -> list[str] | None:
    """Batch-presign artifact URIs, routing screenshots/recordings through the
    TTL-capped variant (SKY-12527). Order-preserving; None when any underlying
    call fails, matching the clients' all-or-nothing batch contract.
    """
    sensitive = [i for i, artifact in enumerate(artifacts) if artifact.artifact_type in SENSITIVE_ARTIFACT_TYPES]
    if not sensitive:
        return await presign([artifact.uri for artifact in artifacts])
    other = [i for i, artifact in enumerate(artifacts) if artifact.artifact_type not in SENSITIVE_ARTIFACT_TYPES]
    urls: list[str | None] = [None] * len(artifacts)
    for indices, mint in ((sensitive, presign_sensitive), (other, presign)):
        if not indices:
            continue
        minted = await mint([artifacts[i].uri for i in indices])
        if minted is None:
            return None
        for index, url in zip(indices, minted, strict=True):
            urls[index] = url
    return cast(list[str], urls)


async def _file_infos_from_artifacts(artifacts: list[Artifact], *, artifact_type: ArtifactType) -> list[FileInfo]:
    """Build the API-shaped ``FileInfo`` list from a homogeneous batch of
    artifact rows (e.g. all DOWNLOAD or all RECORDING).

    Filename is the URI basename (the save site writes ``{base_uri}/{file}``);
    checksum and modified_at come straight from the row, so retrieval needs
    zero S3 round-trips.

    All artifacts in a single batch share the same organization (downloads /
    recordings are scoped to a run or browser session, which is scoped to an
    org), so the per-org URL TTL is resolved once and applied to every URL.

    The ``artifact_type`` is only used for the URL's informational query
    parameter — it does not affect the HMAC signature. Callers must pass rows
    of a single type so the URL hint is correct.
    """
    if not artifacts:
        return []
    organization_id = artifacts[0].organization_id
    expiry_seconds = await app.ARTIFACT_MANAGER.resolve_artifact_url_expiry_seconds(organization_id)
    _ = artifact_type  # kept for call-site compatibility; URL hint now sourced from the artifact row.
    infos: list[FileInfo] = []
    for artifact in artifacts:
        filename = artifact.uri.rsplit("/", 1)[-1] if artifact.uri else ""
        url = await app.ARTIFACT_MANAGER.resolve_share_url(artifact, expiry_seconds=expiry_seconds)
        if url is None:
            continue
        infos.append(
            FileInfo(
                url=url,
                checksum=artifact.checksum,
                filename=filename,
                file_size=artifact.file_size,
                modified_at=artifact.created_at,
                artifact_id=artifact.artifact_id,
            )
        )
    return infos


def download_checksums_by_uri(artifacts: list[Artifact]) -> dict[str, str]:
    """URI -> SHA-256 for DOWNLOAD rows that carry one.

    A row is only written after its bytes land in storage, so a file whose checksum
    matches its row is already saved and the save loop can skip re-uploading it
    (SKY-14752). Rows without a checksum are omitted: they cannot vouch for content.
    """
    return {artifact.uri: artifact.checksum for artifact in artifacts if artifact.uri and artifact.checksum}


def dedupe_run_scoped_download_artifacts(artifacts: list[Artifact]) -> list[Artifact]:
    """Collapse the two representations of one persistent-session download in a run's DOWNLOAD set.

    A download can be registered both session-produced (``browser_session_id`` set — run-bound at
    insert, or claimed later) and run-scoped by the local save (``browser_session_id`` NULL); when both
    carry the same non-null checksum they are the same bytes, so drop the session-produced row and keep
    the run-scoped one as canonical. Pairing is one-for-one: a checksum with N run-scoped rows drops at
    most N session rows, so two legitimate same-content session downloads backed by a single run-scoped
    row keep one. Order is preserved; distinct/absent checksums, twinless session rows, and multiple
    run-scoped rows are all kept. Run-scoped only — never call on a session listing.
    """
    run_scoped_checksums = Counter(a.checksum for a in artifacts if a.browser_session_id is None and a.checksum)
    if not run_scoped_checksums:
        return artifacts
    kept: list[Artifact] = []
    for a in artifacts:
        if a.browser_session_id is not None and a.checksum and run_scoped_checksums[a.checksum] > 0:
            run_scoped_checksums[a.checksum] -= 1
            continue
        kept.append(a)
    return kept


async def _file_infos_from_download_artifacts(artifacts: list[Artifact]) -> list[FileInfo]:
    """Backward-compat alias for DOWNLOAD-typed callers.

    Forwards to :func:`_file_infos_from_artifacts` with the DOWNLOAD type so
    pre-existing import sites keep working without each having to thread the
    artifact_type through.
    """
    return await _file_infos_from_artifacts(artifacts, artifact_type=ArtifactType.DOWNLOAD)


# TODO: This should be a part of the ArtifactType model
FILE_EXTENTSION_MAP: dict[ArtifactType, str] = {
    ArtifactType.RECORDING: "webm",
    ArtifactType.AUDIO: "webm",
    ArtifactType.SESSION_REPLAY: "mp4",
    ArtifactType.BROWSER_CONSOLE_LOG: "log",
    ArtifactType.SCREENSHOT_LLM: "png",
    ArtifactType.SCREENSHOT_ACTION: "png",
    ArtifactType.SCREENSHOT_PRE_SUBMIT: "png",
    ArtifactType.SCREENSHOT_FINAL: "png",
    ArtifactType.SKYVERN_LOG: "log",
    ArtifactType.SKYVERN_LOG_RAW: "json",
    ArtifactType.LLM_PROMPT: "txt",
    ArtifactType.LLM_REQUEST: "json",
    ArtifactType.LLM_RESPONSE: "json",
    ArtifactType.LLM_RESPONSE_PARSED: "json",
    ArtifactType.LLM_RESPONSE_RENDERED: "json",
    ArtifactType.VISIBLE_ELEMENTS_ID_CSS_MAP: "json",
    ArtifactType.VISIBLE_ELEMENTS_ID_FRAME_MAP: "json",
    ArtifactType.VISIBLE_ELEMENTS_TREE: "json",
    ArtifactType.VISIBLE_ELEMENTS_TREE_TRIMMED: "json",
    ArtifactType.VISIBLE_ELEMENTS_TREE_IN_PROMPT: "txt",
    ArtifactType.HTML_SCRAPE: "html",
    ArtifactType.HTML_ACTION: "html",
    ArtifactType.HTML_PRE_SUBMIT: "html",
    ArtifactType.TRACE: "zip",
    ArtifactType.HAR: "har",
    ArtifactType.HASHED_HREF_MAP: "json",
    # DEPRECATED: we're using CSS selector map now
    ArtifactType.VISIBLE_ELEMENTS_ID_XPATH_MAP: "json",
    ArtifactType.PDF: "pdf",
    ArtifactType.STEP_ARCHIVE: "zip",
    ArtifactType.TASK_ARCHIVE: "zip",
}


class BaseStorage(ABC):
    @abstractmethod
    def build_uri(self, *, organization_id: str, artifact_id: str, step: Step, artifact_type: ArtifactType) -> str:
        pass

    @abstractmethod
    async def retrieve_global_workflows(self) -> list[str]:
        pass

    @abstractmethod
    def build_log_uri(
        self, *, organization_id: str, log_entity_type: LogEntityType, log_entity_id: str, artifact_type: ArtifactType
    ) -> str:
        pass

    @abstractmethod
    def build_thought_uri(
        self, *, organization_id: str, artifact_id: str, thought: Thought, artifact_type: ArtifactType
    ) -> str:
        pass

    @abstractmethod
    def build_task_v2_uri(
        self, *, organization_id: str, artifact_id: str, task_v2: TaskV2, artifact_type: ArtifactType
    ) -> str:
        pass

    @abstractmethod
    def build_workflow_run_block_uri(
        self,
        *,
        organization_id: str,
        artifact_id: str,
        workflow_run_block: WorkflowRunBlock,
        artifact_type: ArtifactType,
    ) -> str:
        pass

    @abstractmethod
    def build_ai_suggestion_uri(
        self, *, organization_id: str, artifact_id: str, ai_suggestion: AISuggestion, artifact_type: ArtifactType
    ) -> str:
        pass

    @abstractmethod
    def build_script_file_uri(
        self, *, organization_id: str, script_id: str, script_version: int, file_path: str
    ) -> str:
        pass

    @abstractmethod
    async def store_artifact(
        self,
        artifact: Artifact,
        data: bytes,
        supersede_queued_prefixes: bool = False,
        prefix_uri: str | None = None,
    ) -> None:
        # supersede_queued_prefixes marks a recording's finalize write and prefix_uri names the pre-finalize
        # key its per-step prefixes queued to (when the finalize renames the object, e.g. .webm -> .mp4).
        # Only the S3 backend serializes prefix vs finalize writes and acts on these; other backends ignore
        # them and just overwrite, so the stale-prefix-cannot-overwrite-the-finalized-object ordering
        # guarantee holds on S3 only (see store_artifact_prefix_from_path).
        pass

    @abstractmethod
    async def retrieve_artifact(self, artifact: Artifact) -> bytes | None:
        pass

    async def check_archived_uris(self, uris: list[str]) -> dict[str, bool]:
        """Check whether each URI points to an archived (non-retrievable) S3 object.

        Returns a mapping of URI -> True if the object is in GLACIER or DEEP_ARCHIVE.
        Default implementation returns False for all URIs (local/Azure storage is never archived).
        """
        return {uri: False for uri in uris}

    @abstractmethod
    async def get_share_link(self, artifact: Artifact) -> str | None:
        pass

    @abstractmethod
    async def get_share_links(self, artifacts: list[Artifact]) -> list[str] | None:
        pass

    @abstractmethod
    async def store_artifact_from_path(self, artifact: Artifact, path: str) -> None:
        pass

    async def store_artifact_prefix_from_path(self, artifact: Artifact, path: str, length: int) -> None:
        """Upload exactly the first ``length`` bytes of ``path`` (a snapshot of a still-growing file).

        The default reads that bounded prefix and delegates to ``store_artifact``; backends that can
        stream override this to avoid materializing the prefix in memory. The default buffers, so on
        local/GCS/Azure a per-step prefix is not serialized against the finalize write — only S3 orders
        them so a stale prefix cannot overwrite the finalized object.
        """
        with open(path, "rb") as f:
            data = f.read(length)
        await self.store_artifact(artifact, data)

    @abstractmethod
    async def save_streaming_file(self, organization_id: str, file_name: str) -> bool | None:
        """None/True means the frame was uploaded; False means a gate intentionally skipped it,
        so callers must not record a skipped frame as published."""

    @abstractmethod
    async def get_streaming_file(self, organization_id: str, file_name: str) -> bytes | None:
        pass

    @abstractmethod
    async def store_browser_session(self, organization_id: str, workflow_permanent_id: str, directory: str) -> None:
        pass

    @abstractmethod
    async def retrieve_browser_session(self, organization_id: str, workflow_permanent_id: str) -> str | None:
        pass

    @abstractmethod
    async def delete_browser_session(self, organization_id: str, workflow_permanent_id: str) -> None:
        pass

    @abstractmethod
    async def store_browser_profile(self, organization_id: str, profile_id: str, directory: str) -> None:
        """Store a browser profile from a directory."""

    @abstractmethod
    async def retrieve_browser_profile(self, organization_id: str, profile_id: str) -> str | None:
        """Retrieve a browser profile to a temporary directory."""

    async def browser_profile_exists(self, organization_id: str, profile_id: str) -> bool:
        """Whether a stored profile archive exists (has content). Non-destructive probe — some backends
        (local) return the live directory, not a temp copy, so this must never delete what it finds.
        Backends with a cheap existence check (S3 head, local stat) override this.
        ponytail: for object backends this may download a temp copy; acceptable — only opt-in seed
        workflows reach it, and correctness (never deleting real state) beats saving one download."""
        return bool(await self.retrieve_browser_profile(organization_id, profile_id))

    async def get_browser_profile_etag(self, organization_id: str, profile_id: str) -> str | None:
        """A cheap fingerprint of the stored profile archive that changes iff its bytes change, used by
        the freshness guard to detect a concurrent write between a run's seed and its write-back. None
        means "can't tell" — the caller then falls back to a full write. Object backends override with a
        head request; backends without one (local) leave it None (no concurrent-writer race there)."""
        return None

    @abstractmethod
    async def delete_browser_profile(self, organization_id: str, profile_id: str, hard_delete: bool = False) -> None:
        """Delete a stored browser profile. Best-effort: a missing object is not an error.
        hard_delete purges all object versions (true erasure); only S3 bucket-versioning distinguishes it."""

    @abstractmethod
    async def list_downloaded_files_in_browser_session(
        self, organization_id: str, browser_session_id: str
    ) -> list[str]:
        pass

    @abstractmethod
    async def list_downloading_files_in_browser_session(
        self, organization_id: str, browser_session_id: str
    ) -> list[str]:
        pass

    @abstractmethod
    async def get_shared_downloaded_files_in_browser_session(
        self, organization_id: str, browser_session_id: str
    ) -> list[FileInfo]:
        pass

    @abstractmethod
    async def get_shared_recordings_in_browser_session(
        self, organization_id: str, browser_session_id: str
    ) -> list[FileInfo]:
        pass

    @abstractmethod
    async def save_downloaded_files(
        self,
        organization_id: str,
        run_id: str | None,
    ) -> None:
        """Raises DownloadSaveIncompleteError after the loop when any file could not be fully
        saved and registered; every other file already is when it raises, so the save is
        retryable-incomplete."""

    @abstractmethod
    async def get_downloaded_files(self, organization_id: str, run_id: str | None) -> list[FileInfo]:
        pass

    @abstractmethod
    async def save_legacy_file(
        self, *, organization_id: str, filename: str, fileObj: BinaryIO
    ) -> tuple[str, str] | None:
        pass

    @abstractmethod
    async def delete_legacy_file(self, *, organization_id: str, uri: str) -> None:
        """Delete an uploaded file's bytes.

        Implementations must run ``assert_managed_file_access`` first: the URI reaches here
        from a stored row, and this is the last place a row that points outside the
        organization's prefix can be stopped from deleting another tenant's object. Raises
        PermissionError when the URI is out of bounds.
        """

    @abstractmethod
    async def sync_browser_session_file(
        self,
        organization_id: str,
        browser_session_id: str,
        artifact_type: str,
        local_file_path: str,
        remote_path: str,
        date: str | None = None,
        recording_finalized_at: datetime | None = None,
        producer_run_id: str | None = None,
    ) -> str:
        pass

    @abstractmethod
    async def delete_browser_session_file(
        self,
        organization_id: str,
        browser_session_id: str,
        artifact_type: str,
        remote_path: str,
        date: str | None = None,
    ) -> None:
        pass

    @abstractmethod
    async def browser_session_file_exists(
        self,
        organization_id: str,
        browser_session_id: str,
        artifact_type: str,
        remote_path: str,
        date: str | None = None,
    ) -> bool:
        pass

    @abstractmethod
    def assert_managed_file_access(self, uri: str, organization_id: str) -> None:
        pass

    @abstractmethod
    async def download_managed_file(self, uri: str, organization_id: str) -> bytes | None:
        pass

    @abstractmethod
    async def file_exists(self, uri: str) -> bool:
        pass

    @property
    @abstractmethod
    def storage_type(self) -> str:
        pass
