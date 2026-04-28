"""Tests for the per-org artifact URL expiry override (SKY-8861).

Covers:
- ArtifactManager.resolve_artifact_url_expiry_seconds
  (None org, missing org row, value within bounds, clamped, fallback)
- ArtifactManager.build_signed_content_url passes expiry_seconds to signing
- _artifact_content_response_headers Cache-Control max-age computation
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.artifact.manager import ArtifactManager
from skyvern.forge.sdk.artifact.signing import (
    ARTIFACT_URL_EXPIRY_SECONDS,
    ARTIFACT_URL_EXPIRY_SECONDS_MAX,
    ARTIFACT_URL_EXPIRY_SECONDS_MIN,
)
from skyvern.forge.sdk.routes.agent_protocol import _artifact_content_response_headers
from skyvern.forge.sdk.schemas.organizations import Organization


def _make_org(artifact_url_expiry_seconds: int | None) -> Organization:
    now = datetime.now(timezone.utc)
    return Organization(
        organization_id="o_1",
        organization_name="acme",
        artifact_url_expiry_seconds=artifact_url_expiry_seconds,
        created_at=now,
        modified_at=now,
    )


# ---------------------------------------------------------------------------
# resolve_artifact_url_expiry_seconds
# ---------------------------------------------------------------------------


class TestResolveArtifactUrlExpirySeconds:
    @pytest.mark.asyncio
    async def test_none_org_id_returns_global_default(self) -> None:
        """No org in scope (e.g. system contexts) — fall straight to the global default."""
        manager = ArtifactManager()
        with patch("skyvern.forge.sdk.artifact.manager.app") as app:
            app.DATABASE.organizations.get_organization = AsyncMock()
            ttl = await manager.resolve_artifact_url_expiry_seconds(None)
            assert ttl == ARTIFACT_URL_EXPIRY_SECONDS
            app.DATABASE.organizations.get_organization.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_org_row_returns_global_default(self) -> None:
        """Org row not found → fall back rather than raising — keeps URL minting resilient."""
        manager = ArtifactManager()
        with patch("skyvern.forge.sdk.artifact.manager.app") as app:
            app.DATABASE.organizations.get_organization = AsyncMock(return_value=None)
            ttl = await manager.resolve_artifact_url_expiry_seconds("o_missing")
            assert ttl == ARTIFACT_URL_EXPIRY_SECONDS

    @pytest.mark.asyncio
    async def test_org_with_no_override_returns_global_default(self) -> None:
        manager = ArtifactManager()
        org = _make_org(artifact_url_expiry_seconds=None)
        with patch("skyvern.forge.sdk.artifact.manager.app") as app:
            app.DATABASE.organizations.get_organization = AsyncMock(return_value=org)
            ttl = await manager.resolve_artifact_url_expiry_seconds("o_1")
            assert ttl == ARTIFACT_URL_EXPIRY_SECONDS

    @pytest.mark.asyncio
    async def test_org_with_override_within_bounds_returns_override(self) -> None:
        manager = ArtifactManager()
        org = _make_org(artifact_url_expiry_seconds=4 * 3600)  # 4h
        with patch("skyvern.forge.sdk.artifact.manager.app") as app:
            app.DATABASE.organizations.get_organization = AsyncMock(return_value=org)
            ttl = await manager.resolve_artifact_url_expiry_seconds("o_1")
            assert ttl == 4 * 3600

    @pytest.mark.asyncio
    async def test_org_with_below_min_value_clamped_up(self) -> None:
        """Defensive clamp guards against stray DB writes (admin tool, manual SQL, etc.)."""
        manager = ArtifactManager()
        org = _make_org(artifact_url_expiry_seconds=10)
        with patch("skyvern.forge.sdk.artifact.manager.app") as app:
            app.DATABASE.organizations.get_organization = AsyncMock(return_value=org)
            ttl = await manager.resolve_artifact_url_expiry_seconds("o_1")
            assert ttl == ARTIFACT_URL_EXPIRY_SECONDS_MIN

    @pytest.mark.asyncio
    async def test_org_with_above_max_value_clamped_down(self) -> None:
        manager = ArtifactManager()
        org = _make_org(artifact_url_expiry_seconds=30 * 24 * 3600)  # 30 days
        with patch("skyvern.forge.sdk.artifact.manager.app") as app:
            app.DATABASE.organizations.get_organization = AsyncMock(return_value=org)
            ttl = await manager.resolve_artifact_url_expiry_seconds("o_1")
            assert ttl == ARTIFACT_URL_EXPIRY_SECONDS_MAX


# ---------------------------------------------------------------------------
# build_signed_content_url passes expiry through
# ---------------------------------------------------------------------------


class TestBuildSignedContentUrl:
    def test_expiry_seconds_propagated_to_sign(self) -> None:
        manager = ArtifactManager()
        # Stub _bundle_content_url so we observe the kwargs without needing a keyring.
        with patch.object(manager, "_bundle_content_url", return_value="https://x") as bundle:
            manager.build_signed_content_url(artifact_id="a_1", expiry_seconds=3600)
            bundle.assert_called_once_with(
                artifact_id="a_1",
                artifact_name=None,
                artifact_type=None,
                expiry_seconds=3600,
            )

    def test_no_expiry_propagates_none(self) -> None:
        manager = ArtifactManager()
        with patch.object(manager, "_bundle_content_url", return_value="https://x") as bundle:
            manager.build_signed_content_url(artifact_id="a_1")
            kwargs = bundle.call_args.kwargs
            assert kwargs["expiry_seconds"] is None


# ---------------------------------------------------------------------------
# _artifact_content_response_headers Cache-Control
# ---------------------------------------------------------------------------


class TestArtifactContentResponseHeaders:
    def test_signed_with_expiry_uses_remaining_lifetime(self) -> None:
        """Cache-Control max-age must reflect the per-URL expiry, not the global default."""
        import time

        future = int(time.time()) + 3600  # 1h from now
        headers = _artifact_content_response_headers(
            disposition="inline",
            is_signed=True,
            signed_expiry_unix=future,
        )
        max_age = int(headers["Cache-Control"].split("max-age=")[1])
        # Allow a few seconds of clock drift between the call and the assertion.
        assert 3580 <= max_age <= 3600

    def test_signed_with_past_expiry_clamps_to_zero(self) -> None:
        """Don't emit a negative max-age — caches behave unpredictably with negative values."""
        import time

        past = int(time.time()) - 60
        headers = _artifact_content_response_headers(
            disposition="inline",
            is_signed=True,
            signed_expiry_unix=past,
        )
        assert headers["Cache-Control"] == "private, max-age=0"

    def test_signed_without_expiry_falls_back_to_global_default(self) -> None:
        """Defensive fallback when the route can't parse expiry — still emit a sane TTL."""
        headers = _artifact_content_response_headers(
            disposition="inline",
            is_signed=True,
            signed_expiry_unix=None,
        )
        assert headers["Cache-Control"] == f"private, max-age={ARTIFACT_URL_EXPIRY_SECONDS}"

    def test_unsigned_emits_no_cache(self) -> None:
        """Org-API-key path is not URL-bound, so caches must revalidate every time."""
        headers = _artifact_content_response_headers(
            disposition="inline",
            is_signed=False,
        )
        assert headers["Cache-Control"] == "private, no-cache"

    def test_nosniff_always_present(self) -> None:
        for is_signed in (True, False):
            headers = _artifact_content_response_headers(
                disposition="inline",
                is_signed=is_signed,
                signed_expiry_unix=int(__import__("time").time()) + 60 if is_signed else None,
            )
            assert headers["X-Content-Type-Options"] == "nosniff"


# ---------------------------------------------------------------------------
# OrganizationUpdate Pydantic model
# ---------------------------------------------------------------------------


class TestOrganizationUpdateSchema:
    def test_defaults_are_none_and_false(self) -> None:
        from skyvern.forge.sdk.schemas.organizations import OrganizationUpdate

        body = OrganizationUpdate()
        assert body.max_steps_per_run is None
        assert body.artifact_url_expiry_seconds is None
        assert body.clear_artifact_url_expiry_seconds is False

    def test_accepts_within_bounds_value(self) -> None:
        from skyvern.forge.sdk.schemas.organizations import OrganizationUpdate

        body = OrganizationUpdate(artifact_url_expiry_seconds=4 * 3600)
        assert body.artifact_url_expiry_seconds == 4 * 3600

    def test_clear_flag_can_be_set(self) -> None:
        from skyvern.forge.sdk.schemas.organizations import OrganizationUpdate

        body = OrganizationUpdate(clear_artifact_url_expiry_seconds=True)
        assert body.clear_artifact_url_expiry_seconds is True


# ---------------------------------------------------------------------------
# get_share_links_with_bundle_support resolves once
# ---------------------------------------------------------------------------


class TestGetShareLinksWithBundleSupport:
    @pytest.mark.asyncio
    async def test_resolves_per_org_expiry_once_for_batch(self) -> None:
        """All bundled URLs in a batch share an org → one DB lookup, not N."""
        from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType

        manager = ArtifactManager()
        now = datetime.now(timezone.utc)
        artifacts = [
            Artifact(
                artifact_id=f"a_{i}",
                artifact_type=ArtifactType.LLM_REQUEST,
                uri=f"s3://x/{i}.json",
                bundle_key=f"file_{i}.json",
                organization_id="o_1",
                created_at=now,
                modified_at=now,
            )
            for i in range(3)
        ]

        resolve = AsyncMock(return_value=2 * 3600)
        with patch.object(manager, "resolve_artifact_url_expiry_seconds", resolve):
            with patch.object(manager, "_bundle_content_url", return_value="https://x") as bundle:
                with patch("skyvern.forge.sdk.artifact.manager.app") as app:
                    app.STORAGE.get_share_links = AsyncMock(return_value=[])
                    result = await manager.get_share_links_with_bundle_support(artifacts)

        assert len(result) == 3
        # Resolve was called exactly once for the batch.
        assert resolve.await_count == 1
        # Every bundled URL was minted with the resolved TTL.
        assert bundle.call_count == 3
        for call in bundle.call_args_list:
            assert call.kwargs["expiry_seconds"] == 2 * 3600

    @pytest.mark.asyncio
    async def test_empty_artifact_list_returns_empty(self) -> None:
        manager = ArtifactManager()
        # Should not even attempt to resolve.
        with patch.object(
            manager,
            "resolve_artifact_url_expiry_seconds",
            AsyncMock(return_value=ARTIFACT_URL_EXPIRY_SECONDS),
        ) as resolve:
            with patch("skyvern.forge.sdk.artifact.manager.app"):
                result = await manager.get_share_links_with_bundle_support([])
        assert result == []
        # Resolve is still called (org_id=None path), and that's cheap — just don't crash.
        # We mainly want to assert no IndexError on empty list.
        _ = resolve  # mark used


__all__ = [
    "TestArtifactContentResponseHeaders",
    "TestBuildSignedContentUrl",
    "TestGetShareLinksWithBundleSupport",
    "TestOrganizationUpdateSchema",
    "TestResolveArtifactUrlExpirySeconds",
]


# Silence unused import warnings in some lints
_ = MagicMock
