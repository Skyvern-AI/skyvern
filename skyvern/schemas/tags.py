"""Pydantic schemas for the workflow tagging public API surface."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from skyvern.forge.sdk.workflow.models.validators import (
    RUN_METADATA_MAX_KEYS,
    SKYVERN_TAG_NAMESPACE,
    TAG_DESCRIPTION_MAX_LENGTH,
    TAG_KEY_REGEX,
    normalize_tag_description,
    normalize_tags,
)


def _assert_user_key_writable(key: str) -> None:
    """Raise ValueError when ``key`` is in the reserved ``skyvern.*`` namespace
    or violates the URL-safe key regex. Reused on every public write surface
    (SET, DELETE body, DELETE path, PATCH path) so the namespace boundary
    enforced by ``normalize_tags`` on SET can't be bypassed via the other
    write paths."""
    if not isinstance(key, str):
        raise ValueError("tag key must be a string")
    if key.startswith(SKYVERN_TAG_NAMESPACE):
        raise ValueError(f"tag keys must not start with the reserved '{SKYVERN_TAG_NAMESPACE}' prefix")
    if not TAG_KEY_REGEX.match(key):
        raise ValueError(
            "tag keys must match '^[A-Za-z0-9][A-Za-z0-9_.-]*$' "
            "(alphanumeric, underscore, dot, hyphen; must start with alphanumeric)"
        )


class TagApplyRequest(BaseModel):
    """Body for ``POST /v1/workflows/{wpid}/tags``. Either field may be empty;
    both empty is a valid no-op. Same-key collisions: set wins over delete."""

    tags: dict[str, str] = Field(
        default_factory=dict,
        description="Tags to set (overwrite). Map of key to value.",
    )
    tags_to_delete: list[str] = Field(
        default_factory=list,
        description="Tag keys to soft-delete.",
    )

    @field_validator("tags", mode="before")
    @classmethod
    def _normalize(cls, v: object) -> dict[str, str]:
        # Outer-shape guard: dict-or-None only. Without this a list body would
        # crash inside _normalize_kv_dict's .items() call.
        if v is None:
            return {}
        if not isinstance(v, dict):
            raise ValueError("tags must be a JSON object mapping string keys to string values")
        # Inner-element guard: every key AND value must be a string. JSON can
        # carry non-string VALUES (numbers, bools, null) which would otherwise
        # blow up inside `value.strip()`. Non-string KEYS can't survive JSON
        # serialization but can be constructed via direct model_validate() —
        # covered for defense in depth.
        for k, val in v.items():
            if not isinstance(k, str):
                raise ValueError("tags keys must be strings")
            if not isinstance(val, str):
                raise ValueError("tags values must be strings")
        return normalize_tags(v) or {}

    @field_validator("tags_to_delete", mode="before")
    @classmethod
    def _normalize_deletes(cls, v: object) -> list[str]:
        if v is None:
            return []
        # Strings are iterable, so `[k for k in v]` on a string body
        # silently chars-into-list. Explicit list/tuple guard prevents this.
        if isinstance(v, str) or not isinstance(v, (list, tuple)):
            raise ValueError("tags_to_delete must be a JSON array of strings")
        cleaned: list[str] = []
        for k in v:
            if not isinstance(k, str):
                raise ValueError("tags_to_delete entries must be strings")
            trimmed = k.strip()
            if not trimmed:
                continue
            # Same namespace/regex rules as SET, so the reserved skyvern.* prefix
            # and malformed URL-unsafe keys can't be deleted via the body API.
            _assert_user_key_writable(trimmed)
            cleaned.append(trimmed)
        if len(cleaned) > RUN_METADATA_MAX_KEYS:
            raise ValueError(f"tags_to_delete can include at most {RUN_METADATA_MAX_KEYS} entries")
        return cleaned


class TagResponse(BaseModel):
    """Current state of one tag (``GET /v1/workflows/{wpid}/tags`` row)."""

    model_config = ConfigDict(from_attributes=True)

    value: str
    source: str
    set_at: datetime
    set_by: str


class TagsResponse(BaseModel):
    """Current tag map for a workflow."""

    workflow_permanent_id: str
    tags: dict[str, TagResponse]


class TagHistoryItem(BaseModel):
    """One row from ``GET /v1/workflows/{wpid}/tags/history``."""

    model_config = ConfigDict(from_attributes=True)

    tag_event_id: str
    key: str
    value: str | None
    event_type: str
    source: str
    set_at: datetime
    set_by: str
    superseded_at: datetime | None = None


class TagHistoryResponse(BaseModel):
    workflow_permanent_id: str
    events: list[TagHistoryItem]


class TagKey(BaseModel):
    """Tag-key registry entry."""

    model_config = ConfigDict(from_attributes=True)

    key: str
    description: str | None = None
    # Number of workflows currently carrying this tag. Powers the dropdown count
    # and the delete-key confirmation ("removes it from N workflows").
    workflow_count: int = 0


class TagKeyDeleteResponse(BaseModel):
    """Response for ``DELETE /v1/tag-keys/{key}``."""

    key: str
    removed_from_workflow_count: int


class TagKeyUpdate(BaseModel):
    """Body for ``PATCH /v1/tag-keys/{key}``."""

    description: str | None = Field(
        None,
        description=f"Free-form description (max {TAG_DESCRIPTION_MAX_LENGTH} chars). Pass null to clear.",
    )

    @field_validator("description", mode="before")
    @classmethod
    def _normalize(cls, v: object) -> str | None:
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError("description must be a string")
        return normalize_tag_description(v)


class WorkflowTagsBatchRequest(BaseModel):
    """Body for ``POST /v1/workflow-tags`` (used when the wpid list would
    exceed the URL length cap)."""

    workflow_permanent_ids: list[str] = Field(
        default_factory=list,
        description="Workflow permanent IDs to fetch tags for.",
    )


class WorkflowTagsBatchResponse(BaseModel):
    """Response for the batch endpoint.

    Workflows with no tags are present with an empty dict so the frontend can
    distinguish "fetched, none set" from "not fetched" without a second call.
    Workflows outside the caller's org are silently absent (no leakage)."""

    workflow_tags: dict[str, dict[str, str]]
