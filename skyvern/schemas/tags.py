"""Pydantic schemas for the workflow tagging public API surface."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from skyvern.forge.sdk.workflow.models.validators import (
    RUN_METADATA_MAX_KEYS,
    SKYVERN_TAG_NAMESPACE,
    TAG_DESCRIPTION_MAX_LENGTH,
    TAG_KEY_REGEX,
    normalize_optional_tag_key,
    normalize_optional_tag_value,
    normalize_tag_description,
    normalize_tag_value,
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


class TagInput(BaseModel):
    """One tag to set. ``value`` is the required label; ``key`` is the optional
    group — null for a standalone label, set for a grouped label (e.g. env:prod)."""

    key: str | None = Field(default=None, description="Optional group (key). Omit for a standalone label.")
    value: str = Field(description="Label (value). Always required.")

    @field_validator("key", mode="before")
    @classmethod
    def _normalize_key(cls, v: object) -> str | None:
        return normalize_optional_tag_key(v)

    @field_validator("value", mode="before")
    @classmethod
    def _normalize_value(cls, v: object) -> str:
        return normalize_tag_value(v)

    @model_validator(mode="after")
    def _value_avoids_filter_grammar_sigils(self) -> TagInput:
        if self.key is None and ":" in self.value:
            raise ValueError("standalone label values must not contain ':' (use a group, e.g. key:value)")
        if self.key is not None and self.value == "*":
            raise ValueError("grouped tag values must not be exactly '*' (reserved as the group filter wildcard)")
        return self


class TagDeleteInput(BaseModel):
    """One tag to soft-delete: a grouped tag by its ``key``, or a standalone label
    by its ``value`` (omit the key)."""

    key: str | None = Field(default=None, description="Group (key) to delete. Use for grouped tags.")
    value: str | None = Field(default=None, description="Label (value) to delete. Use for standalone labels.")

    @field_validator("key", mode="before")
    @classmethod
    def _normalize_key(cls, v: object) -> str | None:
        return normalize_optional_tag_key(v)

    @field_validator("value", mode="before")
    @classmethod
    def _normalize_value(cls, v: object) -> str | None:
        return normalize_optional_tag_value(v)

    @model_validator(mode="after")
    def _exactly_one_identity(self) -> TagDeleteInput:
        # Delete a grouped tag by key or a standalone label by value, never both —
        # the both-set case is ambiguous (would silently ignore `value`).
        if self.key is None and self.value is None:
            raise ValueError("each delete target must specify a key (group) or a value (label)")
        if self.key is not None and self.value is not None:
            raise ValueError("a delete target must specify either a key (group) or a value (label), not both")
        return self


class TagApplyRequest(BaseModel):
    """Body for ``POST /v1/workflows/{wpid}/tags``. Either field may be empty (both
    empty is a no-op). On a same-identity collision, set wins over delete."""

    tags: list[TagInput] = Field(
        default_factory=list,
        description="Tags to set (overwrite). List of {key?, value} objects.",
    )
    tags_to_delete: list[TagDeleteInput] = Field(
        default_factory=list,
        description="Tags to soft-delete. List of {key?, value?} targets.",
    )

    @field_validator("tags", mode="before")
    @classmethod
    def _coerce_tags(cls, v: object) -> object:
        if v is None:
            return []
        # Outer-shape guard: a JSON object/string body must fail cleanly (422),
        # not coerce into a single-element list or iterate char-by-char.
        if not isinstance(v, list):
            raise ValueError("tags must be a JSON array of {key, value} objects")
        if len(v) > RUN_METADATA_MAX_KEYS:
            raise ValueError(f"tags can include at most {RUN_METADATA_MAX_KEYS} entries")
        return v

    @field_validator("tags_to_delete", mode="before")
    @classmethod
    def _coerce_deletes(cls, v: object) -> object:
        if v is None:
            return []
        if not isinstance(v, list):
            raise ValueError("tags_to_delete must be a JSON array of {key, value} objects")
        if len(v) > RUN_METADATA_MAX_KEYS:
            raise ValueError(f"tags_to_delete can include at most {RUN_METADATA_MAX_KEYS} entries")
        return v


class TagResponse(BaseModel):
    """Current state of one tag (``GET /v1/workflows/{wpid}/tags`` row).
    ``key`` is null for a standalone label."""

    model_config = ConfigDict(from_attributes=True)

    key: str | None = None
    value: str
    source: str
    set_at: datetime
    set_by: str


class TagsResponse(BaseModel):
    """Current tags for a workflow. A list (not a key-map) so standalone labels,
    which have no key, are representable."""

    workflow_permanent_id: str
    tags: list[TagResponse]


class TagHistoryItem(BaseModel):
    """One row from ``GET /v1/workflows/{wpid}/tags/history``."""

    model_config = ConfigDict(from_attributes=True)

    tag_event_id: str
    key: str | None
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


class TagItem(BaseModel):
    """A single tag (key + label) without per-tag attribution. ``key`` is null
    for a standalone label."""

    key: str | None = None
    value: str


class WorkflowTagsBatchResponse(BaseModel):
    """Response for the batch endpoint.

    Workflows with no tags are present with an empty list so the frontend can
    distinguish "fetched, none set" from "not fetched" without a second call.
    Workflows outside the caller's org are silently absent (no leakage)."""

    workflow_tags: dict[str, list[TagItem]]
