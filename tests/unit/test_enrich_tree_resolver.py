from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.core.skyvern_context import EnrichTreeMode, SkyvernContext
from skyvern.forge.sdk.experimentation import enrich_tree


@pytest.mark.asyncio
async def test_resolve_enrich_tree_for_context_uses_run_distinct_id_and_properties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    class Provider:
        async def get_value_cached(
            self,
            feature_name: str,
            distinct_id: str,
            *,
            properties: dict[str, str],
        ) -> str:
            calls.append(
                {
                    "feature_name": feature_name,
                    "distinct_id": distinct_id,
                    "properties": properties,
                }
            )
            return EnrichTreeMode.ENRICHED_TREE_NO_IMAGES_FALLBACK.value

    monkeypatch.setattr(
        enrich_tree,
        "app",
        SimpleNamespace(EXPERIMENTATION_PROVIDER=Provider()),
    )
    monkeypatch.delenv("FORCE_DISABLE_LLM_SCREENSHOTS", raising=False)
    ctx = SkyvernContext()

    await enrich_tree.resolve_enrich_tree_for_context(
        ctx,
        "workflow-run-id",
        "organization-id",
        workflow_permanent_id="workflow-permanent-id",
        task_url="https://example.test",
    )

    assert ctx.enrich_tree_mode == EnrichTreeMode.ENRICHED_TREE_NO_IMAGES_FALLBACK
    assert calls == [
        {
            "feature_name": "enrich_tree",
            "distinct_id": "workflow-run-id",
            "properties": {
                "organization_id": "organization-id",
                "workflow_permanent_id": "workflow-permanent-id",
                "task_url": "https://example.test",
            },
        }
    ]


@pytest.mark.asyncio
async def test_resolve_enrich_tree_for_context_defaults_invalid_values_to_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Provider:
        async def get_value_cached(
            self,
            _feature_name: str,
            _distinct_id: str,
            *,
            properties: dict[str, str],
        ) -> str:
            assert properties == {"organization_id": "organization-id"}
            return "not-a-real-mode"

    monkeypatch.setattr(
        enrich_tree,
        "app",
        SimpleNamespace(EXPERIMENTATION_PROVIDER=Provider()),
    )
    monkeypatch.delenv("FORCE_DISABLE_LLM_SCREENSHOTS", raising=False)
    ctx = SkyvernContext(enrich_tree_mode=EnrichTreeMode.ENRICHED_TREE_NO_IMAGES)

    await enrich_tree.resolve_enrich_tree_for_context(ctx, "task-id", "organization-id")

    assert ctx.enrich_tree_mode == EnrichTreeMode.CONTROL


@pytest.mark.asyncio
async def test_force_disable_llm_screenshots_maps_to_enriched_tree_no_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Provider:
        async def get_value_cached(self, *_args, **_kwargs) -> str:
            raise AssertionError("PostHog should not be called when FORCE_DISABLE_LLM_SCREENSHOTS is set")

    monkeypatch.setattr(
        enrich_tree,
        "app",
        SimpleNamespace(EXPERIMENTATION_PROVIDER=Provider()),
    )
    monkeypatch.setenv("FORCE_DISABLE_LLM_SCREENSHOTS", "true")
    ctx = SkyvernContext()

    await enrich_tree.resolve_enrich_tree_for_context(ctx, "task-id", "organization-id")

    assert ctx.enrich_tree_mode == EnrichTreeMode.ENRICHED_TREE_NO_IMAGES
