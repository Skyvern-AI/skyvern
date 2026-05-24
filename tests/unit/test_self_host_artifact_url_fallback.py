"""Self-host artifact URL fallback.

When ``ARTIFACT_CONTENT_HMAC_KEYRING`` is unset:
1. Bundling is skipped at step-archive flush — each artifact gets its own URI.
2. URL minting falls back to ``STORAGE.get_share_link[s]`` (presigned).

When it is set: today's cloud behavior (bundling on, Skyvern signed URLs).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.artifact.manager import ArtifactManager, _bundling_enabled
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.artifact.storage.test_helpers import create_fake_step

_DUMMY_KEYRING_JSON = '{"current_kid":"k1","keys":{"k1":{"secret":"deadbeef"}}}'


def _artifact(artifact_id: str, *, bundle_key: str | None = None, uri: str | None = None) -> Artifact:
    now = datetime.now(timezone.utc)
    return Artifact(
        artifact_id=artifact_id,
        artifact_type=ArtifactType.SCREENSHOT_ACTION,
        uri=uri or f"s3://bucket/{artifact_id}.png",
        bundle_key=bundle_key,
        organization_id="o_1",
        created_at=now,
        modified_at=now,
    )


class TestBundlingEnabledPredicate:
    def test_disabled_when_keyring_is_none(self) -> None:
        with patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", None):
            assert _bundling_enabled() is False

    def test_disabled_when_keyring_is_empty_string(self) -> None:
        with patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", ""):
            assert _bundling_enabled() is False

    def test_enabled_when_keyring_is_set(self) -> None:
        with patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", _DUMMY_KEYRING_JSON):
            assert _bundling_enabled() is True


class TestResolveShareUrl:
    @pytest.mark.asyncio
    async def test_keyring_set_non_bundled_returns_signed_url(self) -> None:
        manager = ArtifactManager()
        artifact = _artifact("a_1")
        with (
            patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", _DUMMY_KEYRING_JSON),
            patch.object(
                manager, "_bundle_content_url", return_value="https://api/v1/artifacts/a_1/content?sig=x"
            ) as bundle,
            patch("skyvern.forge.sdk.artifact.manager.app") as app,
        ):
            app.STORAGE.get_share_link = AsyncMock()
            url = await manager.resolve_share_url(artifact, expiry_seconds=3600)
        assert url == "https://api/v1/artifacts/a_1/content?sig=x"
        bundle.assert_called_once()
        app.STORAGE.get_share_link.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_keyring_set_bundled_returns_signed_url(self) -> None:
        manager = ArtifactManager()
        artifact = _artifact("a_b", bundle_key="screenshot_action_0.png")
        with (
            patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", _DUMMY_KEYRING_JSON),
            patch.object(
                manager, "_bundle_content_url", return_value="https://api/v1/artifacts/a_b/content?sig=x"
            ) as bundle,
            patch("skyvern.forge.sdk.artifact.manager.app") as app,
        ):
            app.STORAGE.get_share_link = AsyncMock()
            url = await manager.resolve_share_url(artifact, expiry_seconds=3600)
        assert url == "https://api/v1/artifacts/a_b/content?sig=x"
        bundle.assert_called_once()
        assert bundle.call_args.kwargs["artifact_name"] == "screenshot_action_0.png"
        app.STORAGE.get_share_link.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_keyring_set_non_bundled_derives_artifact_name_from_uri(self) -> None:
        """Frontend parses ?artifact_name= out of the URL. Non-bundled artifacts have
        no bundle_key, so we must fall back to the URI basename — otherwise the path
        basename is "content" and the UI falls back to a literal "download" label."""
        manager = ArtifactManager()
        artifact = _artifact("a_dl", uri="s3://bucket/downloads/o_1/wr_1/invoice-2026.pdf")
        with (
            patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", _DUMMY_KEYRING_JSON),
            patch.object(manager, "_bundle_content_url", return_value="https://api/x") as bundle,
        ):
            await manager.resolve_share_url(artifact, expiry_seconds=3600)
        assert bundle.call_args.kwargs["artifact_name"] == "invoice-2026.pdf"

    @pytest.mark.asyncio
    async def test_keyring_unset_non_bundled_returns_storage_presigned(self) -> None:
        manager = ArtifactManager()
        artifact = _artifact("a_2")
        with (
            patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", None),
            patch.object(manager, "_bundle_content_url") as bundle,
            patch("skyvern.forge.sdk.artifact.manager.app") as app,
        ):
            app.STORAGE.get_share_link = AsyncMock(
                return_value="https://bucket.s3.amazonaws.com/...?X-Amz-Signature=abc"
            )
            url = await manager.resolve_share_url(artifact, expiry_seconds=3600)
        assert url == "https://bucket.s3.amazonaws.com/...?X-Amz-Signature=abc"
        app.STORAGE.get_share_link.assert_awaited_once_with(artifact)
        bundle.assert_not_called()

    @pytest.mark.asyncio
    async def test_keyring_unset_bundled_legacy_row_routes_through_signed_url(self) -> None:
        """Safety net: legacy rows with bundle_key set must NOT be presigned —
        their uri points at the ZIP, not the member. Route through the Skyvern
        endpoint (which 403s in webhooks but at least doesn't silently return
        the wrong bytes)."""
        manager = ArtifactManager()
        artifact = _artifact("a_legacy", bundle_key="screenshot_action_0.png")
        with (
            patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", None),
            patch.object(
                manager, "_bundle_content_url", return_value="https://api/v1/artifacts/a_legacy/content"
            ) as bundle,
            patch("skyvern.forge.sdk.artifact.manager.app") as app,
        ):
            app.STORAGE.get_share_link = AsyncMock()
            url = await manager.resolve_share_url(artifact, expiry_seconds=3600)
        assert url == "https://api/v1/artifacts/a_legacy/content"
        bundle.assert_called_once()
        app.STORAGE.get_share_link.assert_not_awaited()


class TestGetShareLinksBatchedFallback:
    @pytest.mark.asyncio
    async def test_keyring_unset_batches_through_storage_get_share_links(self) -> None:
        manager = ArtifactManager()
        artifacts = [_artifact(f"a_{i}") for i in range(3)]
        resolve = AsyncMock(return_value=12 * 3600)
        with (
            patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", None),
            patch.object(manager, "resolve_artifact_url_expiry_seconds", resolve),
            patch.object(manager, "_bundle_content_url") as bundle,
            patch("skyvern.forge.sdk.artifact.manager.app") as app,
        ):
            app.STORAGE.get_share_links = AsyncMock(
                return_value=[
                    "https://bucket.s3.amazonaws.com/a_0?sig=p0",
                    "https://bucket.s3.amazonaws.com/a_1?sig=p1",
                    "https://bucket.s3.amazonaws.com/a_2?sig=p2",
                ]
            )
            result = await manager.get_share_links_with_bundle_support(artifacts)
        assert result == [
            "https://bucket.s3.amazonaws.com/a_0?sig=p0",
            "https://bucket.s3.amazonaws.com/a_1?sig=p1",
            "https://bucket.s3.amazonaws.com/a_2?sig=p2",
        ]
        app.STORAGE.get_share_links.assert_awaited_once_with(artifacts)
        bundle.assert_not_called()
        resolve.assert_awaited_once_with("o_1")

    @pytest.mark.asyncio
    async def test_keyring_unset_storage_returns_none_yields_all_none(self) -> None:
        manager = ArtifactManager()
        artifacts = [_artifact("a_0"), _artifact("a_1")]
        with (
            patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", None),
            patch.object(manager, "resolve_artifact_url_expiry_seconds", AsyncMock(return_value=3600)),
            patch("skyvern.forge.sdk.artifact.manager.app") as app,
        ):
            app.STORAGE.get_share_links = AsyncMock(return_value=None)
            result = await manager.get_share_links_with_bundle_support(artifacts)
        assert result == [None, None]

    @pytest.mark.asyncio
    async def test_keyring_unset_empty_input_returns_empty(self) -> None:
        manager = ArtifactManager()
        with patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", None):
            result = await manager.get_share_links_with_bundle_support([])
        assert result == []

    @pytest.mark.asyncio
    async def test_keyring_unset_get_share_link_single_uses_storage(self) -> None:
        manager = ArtifactManager()
        artifact = _artifact("a_solo")
        with (
            patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", None),
            patch.object(manager, "resolve_artifact_url_expiry_seconds", AsyncMock(return_value=3600)),
            patch("skyvern.forge.sdk.artifact.manager.app") as app,
        ):
            app.STORAGE.get_share_link = AsyncMock(return_value="https://bucket/a_solo?sig=p")
            url = await manager.get_share_link(artifact)
        assert url == "https://bucket/a_solo?sig=p"
        app.STORAGE.get_share_link.assert_awaited_once_with(artifact)

    @pytest.mark.asyncio
    async def test_keyring_unset_mixed_batch_legacy_bundled_routes_to_signed(self) -> None:
        manager = ArtifactManager()
        artifacts = [
            _artifact("a_plain"),
            _artifact("a_legacy_bundle", bundle_key="screenshot_action_0.png"),
            _artifact("a_plain2"),
        ]
        with (
            patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", None),
            patch.object(manager, "resolve_artifact_url_expiry_seconds", AsyncMock(return_value=3600)),
            patch.object(
                manager,
                "_bundle_content_url",
                side_effect=lambda artifact_id, **_: f"https://api/v1/artifacts/{artifact_id}/content",
            ) as bundle,
            patch("skyvern.forge.sdk.artifact.manager.app") as app,
        ):
            app.STORAGE.get_share_links = AsyncMock(
                return_value=[
                    "https://bucket/a_plain?sig=1",
                    "https://bucket/a_plain2?sig=2",
                ]
            )
            result = await manager.get_share_links_with_bundle_support(artifacts)

        assert result == [
            "https://bucket/a_plain?sig=1",
            "https://api/v1/artifacts/a_legacy_bundle/content",
            "https://bucket/a_plain2?sig=2",
        ]
        bundle.assert_called_once()
        # Non-bundled list passed verbatim, preserving input order.
        app.STORAGE.get_share_links.assert_awaited_once()
        passed = app.STORAGE.get_share_links.await_args.args[0]
        assert [a.artifact_id for a in passed] == ["a_plain", "a_plain2"]


class TestFileInfosFromArtifactsRespectsKeyring:
    @pytest.mark.asyncio
    async def test_keyring_unset_yields_storage_presigned_url(self) -> None:
        from skyvern.forge.sdk.artifact.storage.base import _file_infos_from_artifacts

        artifact = _artifact("a_dl", uri="azure://container/o_1/wr_1/invoice.pdf")
        with (
            patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", None),
            patch("skyvern.forge.sdk.artifact.storage.base.app") as app,
        ):
            app.ARTIFACT_MANAGER.resolve_artifact_url_expiry_seconds = AsyncMock(return_value=3600)
            app.ARTIFACT_MANAGER.resolve_share_url = AsyncMock(
                return_value="https://account.blob.core.windows.net/container/invoice.pdf?sas=abc"
            )
            infos = await _file_infos_from_artifacts([artifact], artifact_type=ArtifactType.DOWNLOAD)
        assert len(infos) == 1
        assert infos[0].url == "https://account.blob.core.windows.net/container/invoice.pdf?sas=abc"
        app.ARTIFACT_MANAGER.resolve_share_url.assert_awaited_once()


class TestFlushStepArchiveUnbundled:
    @pytest.mark.asyncio
    async def test_unbundled_flush_writes_one_artifact_per_member(self) -> None:
        """Keyring unset → no ZIP, no STEP_ARCHIVE parent, no bundle_key on members."""
        manager = ArtifactManager()
        step = create_fake_step("step_unbundled_1")
        manager.accumulate_screenshot_to_step_archive(
            step=step, screenshots=[b"png0", b"png1"], artifact_type=ArtifactType.SCREENSHOT_ACTION
        )
        manager.accumulate_scrape_to_archive(
            step=step,
            html=b"<html/>",
            id_css_map=b"{}",
            id_frame_map=b"{}",
            element_tree=b"{}",
            element_tree_trimmed=b"{}",
            element_tree_in_prompt=b"",
        )

        bulk_create = AsyncMock()
        store = AsyncMock()
        build_uri = MagicMock(side_effect=lambda **kw: f"s3://bucket/{kw['artifact_id']}.bin")

        with (
            patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", None),
            patch("skyvern.forge.sdk.artifact.manager.app") as app,
        ):
            app.DATABASE.artifacts.bulk_create_artifacts = bulk_create
            app.STORAGE.store_artifact = store
            app.STORAGE.build_uri = build_uri
            await manager.flush_step_archive("step_unbundled_1")

        # Eight members: 2 screenshots + 6 scrape entries.
        assert store.await_count == 8
        bulk_create.assert_awaited_once()
        models = bulk_create.await_args.args[0]
        assert len(models) == 8  # No parent STEP_ARCHIVE row.
        assert all(m.bundle_key is None for m in models)
        assert all(m.artifact_type != ArtifactType.STEP_ARCHIVE for m in models)
        called_types = [call.kwargs["artifact_type"] for call in build_uri.call_args_list]
        assert ArtifactType.SCREENSHOT_ACTION in called_types
        assert ArtifactType.HTML_SCRAPE in called_types

    @pytest.mark.asyncio
    async def test_bundled_flush_unchanged_when_keyring_set(self) -> None:
        manager = ArtifactManager()
        step = create_fake_step("step_bundled_1")
        manager.accumulate_screenshot_to_step_archive(
            step=step, screenshots=[b"a"], artifact_type=ArtifactType.SCREENSHOT_ACTION
        )

        bulk_create = AsyncMock()
        store = AsyncMock()
        build_uri = MagicMock(return_value="s3://bucket/parent.zip")

        with (
            patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", _DUMMY_KEYRING_JSON),
            patch("skyvern.forge.sdk.artifact.manager.app") as app,
        ):
            app.DATABASE.artifacts.bulk_create_artifacts = bulk_create
            app.STORAGE.store_artifact = store
            app.STORAGE.build_uri = build_uri
            await manager.flush_step_archive("step_bundled_1")

        store.assert_awaited_once()
        bulk_create.assert_awaited_once()
        models = bulk_create.await_args.args[0]
        assert len(models) == 2
        assert models[0].artifact_type == ArtifactType.STEP_ARCHIVE
        assert models[0].bundle_key is None
        assert models[1].bundle_key == "screenshot_action_0.png"

    @pytest.mark.asyncio
    async def test_unbundled_flush_applies_pending_screenshot_fk_updates(self) -> None:
        """Deferred action.screenshot_artifact_id writes must still fire in the unbundled path."""
        manager = ArtifactManager()
        step = create_fake_step("step_unbundled_fk")
        ids = manager.accumulate_screenshot_to_step_archive(
            step=step, screenshots=[b"png"], artifact_type=ArtifactType.SCREENSHOT_ACTION
        )
        acc = manager._step_archives["step_unbundled_fk"]
        acc.pending_action_screenshot_updates.append((step.organization_id, "act_1", ids[0]))

        update_fk = AsyncMock()
        with (
            patch.object(settings, "ARTIFACT_CONTENT_HMAC_KEYRING", None),
            patch("skyvern.forge.sdk.artifact.manager.app") as app,
        ):
            app.DATABASE.artifacts.bulk_create_artifacts = AsyncMock()
            app.DATABASE.artifacts.update_action_screenshot_artifact_id = update_fk
            app.STORAGE.store_artifact = AsyncMock()
            app.STORAGE.build_uri = MagicMock(return_value="s3://bucket/x.bin")
            await manager.flush_step_archive("step_unbundled_fk")

        update_fk.assert_awaited_once_with(
            organization_id=step.organization_id,
            action_id="act_1",
            screenshot_artifact_id=ids[0],
        )
