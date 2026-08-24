from __future__ import annotations

import re
from collections.abc import Iterator
from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    from skyvern.forge.sdk.schemas.organizations import Organization

ORG_SLUG_MAX_LENGTH = 20
ORG_SLUG_MAX_SUFFIX_ATTEMPTS = 5
ORG_SLUG_UNIQUE_CONSTRAINT = "uq_organizations_slug"
_ORG_SLUG_SQLITE_UNIQUE_ERROR = "UNIQUE constraint failed: organizations.slug"
ORG_SLUG_PATTERN = re.compile(r"^[a-z0-9-]{1,20}$")
_INVALID_CHARACTER_PATTERN = re.compile(r"[^a-z0-9-]")
_CONSECUTIVE_HYPHEN_PATTERN = re.compile(r"-+")


def is_valid_org_slug(value: str) -> bool:
    return ORG_SLUG_PATTERN.fullmatch(value) is not None


def _normalize_slug_source(value: str) -> str:
    normalized = _INVALID_CHARACTER_PATTERN.sub("-", value.casefold())
    return _CONSECUTIVE_HYPHEN_PATTERN.sub("-", normalized).strip("-")


def sanitize_org_slug(value: str, organization_id: str) -> str:
    """Return an address-safe slug, using the organization ID when the value is empty."""
    slug = _normalize_slug_source(value)[:ORG_SLUG_MAX_LENGTH]
    if slug:
        return slug

    organization_id_tail = _normalize_slug_source(organization_id)[-ORG_SLUG_MAX_LENGTH:]
    if not organization_id_tail:
        raise ValueError("organization_id must contain at least one ASCII letter or digit")
    return organization_id_tail[-ORG_SLUG_MAX_LENGTH:]


def build_org_id_tail_slug(organization_id: str) -> str:
    return sanitize_org_slug("", organization_id)


def is_org_slug_unique_violation(error: IntegrityError) -> bool:
    """Return whether an integrity error identifies the organization slug constraint."""
    diagnostic = getattr(error.orig, "diag", None)
    constraint_name = getattr(diagnostic, "constraint_name", None)
    if constraint_name is not None:
        return constraint_name == ORG_SLUG_UNIQUE_CONSTRAINT
    original_error = str(error.orig)
    return (
        ORG_SLUG_UNIQUE_CONSTRAINT in original_error
        or ORG_SLUG_UNIQUE_CONSTRAINT in str(error)
        or _ORG_SLUG_SQLITE_UNIQUE_ERROR in original_error
    )


def derive_org_slug(organization_name: str, organization_id: str) -> str:
    return sanitize_org_slug(organization_name, organization_id)


def build_org_slug_candidate(base_slug: str, collision_number: int) -> str:
    """Build the base slug, then ``-2``, ``-3``, and later collision candidates."""
    if not is_valid_org_slug(base_slug):
        raise ValueError("base_slug must match ^[a-z0-9-]{1,20}$")
    if collision_number < 1:
        raise ValueError("collision_number must be at least 1")
    if collision_number == 1:
        return base_slug

    suffix = f"-{collision_number}"
    prefix_length = ORG_SLUG_MAX_LENGTH - len(suffix)
    if prefix_length < 1:
        raise ValueError("collision_number is too large to fit in an organization slug")
    prefix = base_slug[:prefix_length].rstrip("-")
    if not prefix:
        prefix = base_slug[:prefix_length]
    return f"{prefix}{suffix}"


def iter_org_slug_candidates(base_slug: str, organization_id: str) -> Iterator[str]:
    """Yield the bounded base ladder, followed by the organization ID tail."""
    organization_id_tail = build_org_id_tail_slug(organization_id)
    seen: set[str] = set()
    if base_slug:
        for collision_number in range(1, ORG_SLUG_MAX_SUFFIX_ATTEMPTS + 2):
            candidate = build_org_slug_candidate(base_slug, collision_number)
            if candidate not in seen:
                seen.add(candidate)
                yield candidate
    if organization_id_tail not in seen:
        yield organization_id_tail


class OrganizationSlugCollisionError(RuntimeError):
    pass


async def persist_org_slug(organization: Organization, base_slug: str) -> str:
    """Persist a slug with bounded retries without replacing a concurrent winner."""
    from skyvern.forge import app

    last_collision: IntegrityError | None = None
    for candidate in iter_org_slug_candidates(base_slug, organization.organization_id):
        try:
            if organization.slug is None:
                stored_organization = await app.DATABASE.organizations.set_organization_slug_if_missing(
                    organization_id=organization.organization_id,
                    slug=candidate,
                )
            else:
                stored_organization = await app.DATABASE.organizations.update_organization(
                    organization.organization_id,
                    slug=candidate,
                    update_slug=True,
                )
        except IntegrityError as exc:
            if not is_org_slug_unique_violation(exc):
                raise
            last_collision = exc
            continue
        if stored_organization.slug is None:
            raise RuntimeError("organization slug persistence returned no slug")
        return stored_organization.slug

    raise OrganizationSlugCollisionError(
        f"Could not assign a unique slug to organization {organization.organization_id}"
    ) from last_collision


async def get_or_derive_org_slug(organization: Organization) -> str:
    if organization.slug:
        return organization.slug
    base_slug = derive_org_slug(organization.organization_name, organization.organization_id)
    return await persist_org_slug(organization, base_slug)
