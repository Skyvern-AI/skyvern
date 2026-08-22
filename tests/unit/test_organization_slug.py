from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from skyvern.forge.sdk.schemas.organizations import OrganizationUpdate
from skyvern.utils.organization_slug import (
    build_org_slug_candidate,
    derive_org_slug,
    get_or_derive_org_slug,
    iter_org_slug_candidates,
)


def _integrity_error(constraint_name: str) -> IntegrityError:
    return IntegrityError(
        "UPDATE organizations",
        {},
        Exception(f'duplicate key value violates unique constraint "{constraint_name}"'),
    )


@pytest.mark.parametrize(
    ("organization_name", "organization_id", "expected"),
    [
        ("Acme CORP", "o_unused", "acme-corp"),
        ("Straße Labs", "o_unused", "strasse-labs"),
        ("___Acme!!! --  Corp___", "o_unused", "acme-corp"),
        ("Acme.Inc", "o_unused", "acme-inc"),
        ("AcmeInc", "o_unused", "acmeinc"),
        ("Acme💥Inc", "o_unused", "acme-inc"),
        ("abcdefghijklmnopqrstuvwxyz", "o_unused", "abcdefghijklmnopqrst"),
        ("!!!", "o_1234567890ABCDEFGHIJKL", "34567890abcdefghijkl"),
    ],
)
def test_derive_org_slug_rules(organization_name: str, organization_id: str, expected: str) -> None:
    assert derive_org_slug(organization_name, organization_id) == expected


def test_invalid_character_mapping_keeps_acme_dot_inc_distinct_from_acme_inc() -> None:
    assert derive_org_slug("Acme.Inc", "o_unused") == "acme-inc"
    assert derive_org_slug("AcmeInc", "o_unused") == "acmeinc"


def test_collision_candidates_preserve_max_length_and_include_org_id_tail() -> None:
    base_slug = "abcdefghijklmnopqrst"
    organization_id = "o_1234567890abcdef"

    assert build_org_slug_candidate(base_slug, 1) == base_slug
    assert build_org_slug_candidate(base_slug, 2) == "abcdefghijklmnopqr-2"
    assert list(iter_org_slug_candidates(base_slug, organization_id)) == [
        base_slug,
        "abcdefghijklmnopqr-2",
        "abcdefghijklmnopqr-3",
        "abcdefghijklmnopqr-4",
        "abcdefghijklmnopqr-5",
        "abcdefghijklmnopqr-6",
        "o-1234567890abcdef",
    ]
    assert list(iter_org_slug_candidates("", organization_id)) == ["o-1234567890abcdef"]
    assert list(iter_org_slug_candidates("o-123", "o_123")) == [
        "o-123",
        "o-123-2",
        "o-123-3",
        "o-123-4",
        "o-123-5",
        "o-123-6",
    ]
    assert list(iter_org_slug_candidates("o", "o_2")) == [
        "o",
        "o-2",
        "o-3",
        "o-4",
        "o-5",
        "o-6",
    ]


@pytest.mark.asyncio
async def test_get_or_derive_org_slug_retries_slug_unique_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge import app

    organization = SimpleNamespace(
        organization_id="o_123",
        organization_name="Acme",
        slug=None,
    )
    set_slug_if_missing = AsyncMock(
        side_effect=[
            _integrity_error("uq_organizations_slug"),
            SimpleNamespace(slug="acme-2"),
        ]
    )
    monkeypatch.setattr(
        app,
        "DATABASE",
        SimpleNamespace(organizations=SimpleNamespace(set_organization_slug_if_missing=set_slug_if_missing)),
    )

    assert await get_or_derive_org_slug(organization) == "acme-2"
    assert set_slug_if_missing.await_args_list[0].kwargs == {
        "organization_id": "o_123",
        "slug": "acme",
    }
    assert set_slug_if_missing.await_args_list[1].kwargs == {
        "organization_id": "o_123",
        "slug": "acme-2",
    }


@pytest.mark.asyncio
async def test_get_or_derive_org_slug_uses_org_id_tail_after_five_suffix_collisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.forge import app

    organization = SimpleNamespace(
        organization_id="o_1234567890abcdef",
        organization_name="Acme",
        slug=None,
    )
    set_slug_if_missing = AsyncMock(
        side_effect=[
            *[_integrity_error("uq_organizations_slug") for _ in range(6)],
            SimpleNamespace(slug="o-1234567890abcdef"),
        ]
    )
    monkeypatch.setattr(
        app,
        "DATABASE",
        SimpleNamespace(organizations=SimpleNamespace(set_organization_slug_if_missing=set_slug_if_missing)),
    )

    assert await get_or_derive_org_slug(organization) == "o-1234567890abcdef"
    assert [call.kwargs["slug"] for call in set_slug_if_missing.await_args_list] == [
        "acme",
        "acme-2",
        "acme-3",
        "acme-4",
        "acme-5",
        "acme-6",
        "o-1234567890abcdef",
    ]


@pytest.mark.asyncio
async def test_get_or_derive_org_slug_preserves_concurrent_winner(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge import app

    organization = SimpleNamespace(
        organization_id="o_123",
        organization_name="Acme",
        slug=None,
    )
    set_slug_if_missing = AsyncMock(return_value=SimpleNamespace(slug="newer-clerk-slug"))
    monkeypatch.setattr(
        app,
        "DATABASE",
        SimpleNamespace(organizations=SimpleNamespace(set_organization_slug_if_missing=set_slug_if_missing)),
    )

    assert await get_or_derive_org_slug(organization) == "newer-clerk-slug"
    set_slug_if_missing.assert_awaited_once_with(organization_id="o_123", slug="acme")


@pytest.mark.asyncio
async def test_get_or_derive_org_slug_does_not_retry_other_integrity_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.forge import app

    organization = SimpleNamespace(
        organization_id="o_123",
        organization_name="Acme",
        slug=None,
    )
    error = _integrity_error("uq_organizations_domain")
    set_slug_if_missing = AsyncMock(side_effect=error)
    monkeypatch.setattr(
        app,
        "DATABASE",
        SimpleNamespace(organizations=SimpleNamespace(set_organization_slug_if_missing=set_slug_if_missing)),
    )

    with pytest.raises(IntegrityError) as exc_info:
        await get_or_derive_org_slug(organization)

    assert exc_info.value is error
    set_slug_if_missing.assert_awaited_once()


@pytest.mark.parametrize("slug", ["a", "acme-2", "abcdefghijklmnopqrst", None])
def test_organization_update_accepts_valid_slug(slug: str | None) -> None:
    update = OrganizationUpdate(slug=slug)

    assert update.slug == slug
    assert "slug" in update.model_fields_set


@pytest.mark.parametrize(
    "slug",
    ["", "Acme", "acme_org", "acme.org", "acme org", "abcdefghijklmnopqrstu"],
)
def test_organization_update_rejects_invalid_slug(slug: str) -> None:
    with pytest.raises(ValidationError, match="slug must match"):
        OrganizationUpdate(slug=slug)


@pytest.mark.parametrize(
    "error",
    [
        _integrity_error("uq_organizations_slug"),
        IntegrityError("UPDATE organizations", {}, Exception("UNIQUE constraint failed: organizations.slug")),
    ],
    ids=["postgresql", "sqlite"],
)
@pytest.mark.asyncio
async def test_update_organization_returns_conflict_for_duplicate_slug(
    monkeypatch: pytest.MonkeyPatch,
    error: IntegrityError,
) -> None:
    from skyvern.forge.sdk.routes import agent_protocol

    update_organization = AsyncMock(side_effect=error)
    monkeypatch.setattr(
        agent_protocol.app,
        "DATABASE",
        SimpleNamespace(organizations=SimpleNamespace(update_organization=update_organization)),
    )

    with pytest.raises(HTTPException) as exc_info:
        await agent_protocol.update_organization(
            OrganizationUpdate(slug="taken"),
            current_org=SimpleNamespace(organization_id="o_123", webhook_callback_url=None),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Organization slug is already in use."


@pytest.mark.asyncio
async def test_update_organization_reraises_unrelated_integrity_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.routes import agent_protocol

    error = _integrity_error("uq_organizations_domain")
    update_organization = AsyncMock(side_effect=error)
    monkeypatch.setattr(
        agent_protocol.app,
        "DATABASE",
        SimpleNamespace(organizations=SimpleNamespace(update_organization=update_organization)),
    )

    with pytest.raises(IntegrityError) as exc_info:
        await agent_protocol.update_organization(
            OrganizationUpdate(slug="available"),
            current_org=SimpleNamespace(organization_id="o_123", webhook_callback_url=None),
        )

    assert exc_info.value is error


@pytest.mark.asyncio
async def test_update_organization_rejects_explicit_slug_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.routes import agent_protocol

    update_organization = AsyncMock()
    invalidate_cached_org = MagicMock()
    monkeypatch.setattr(
        agent_protocol.app,
        "DATABASE",
        SimpleNamespace(organizations=SimpleNamespace(update_organization=update_organization)),
    )
    monkeypatch.setattr(agent_protocol.org_auth_service, "invalidate_cached_org", invalidate_cached_org)

    with pytest.raises(HTTPException) as exc_info:
        await agent_protocol.update_organization(
            OrganizationUpdate(slug=None),
            current_org=SimpleNamespace(organization_id="o_123", webhook_callback_url=None),
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Organization slug cannot be cleared."
    update_organization.assert_not_awaited()
    invalidate_cached_org.assert_not_called()
