from io import BytesIO
from types import SimpleNamespace

from PIL import Image

from skyvern.utils.image_token_estimator import (
    estimate_image_cost,
    estimate_image_tokens,
    provider_image_tokens,
)


def _response(image_tokens: object) -> SimpleNamespace:
    details = SimpleNamespace(image_tokens=image_tokens)
    return SimpleNamespace(usage=SimpleNamespace(prompt_tokens_details=details))


def _png(width: int, height: int) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="PNG")
    return buf.getvalue()


def test_empty_or_none_screenshots_are_zero() -> None:
    assert estimate_image_tokens(None, "gemini-2.5-flash") == 0
    assert estimate_image_tokens([], "gemini-2.5-flash") == 0


def test_gemini_tiling_scales_with_resolution() -> None:
    # 1920x1080 -> ceil(1920/768)=3 * ceil(1080/768)=2 = 6 tiles * 258
    assert estimate_image_tokens([_png(1920, 1080)], "gemini-2.5-flash-lite") == 6 * 258
    # 1280x720 -> 2 * 1 = 2 tiles * 258
    assert estimate_image_tokens([_png(1280, 720)], "vertex_ai/gemini-3-flash") == 2 * 258
    # both sides <= 384 -> flat 258
    assert estimate_image_tokens([_png(300, 300)], "gemini-2.5-flash") == 258


def test_openai_tile_models_normalize_short_side_to_768() -> None:
    # legacy tile-based models (gpt-4o, gpt-4.1): short side -> 768 => 3*2 = 6 tiles
    assert estimate_image_tokens([_png(1920, 1080)], "gpt-4o") == 85 + 170 * 6
    # 1280x720 short side already < 768 => still 6 tiles (720p saves nothing on tile models)
    assert estimate_image_tokens([_png(1280, 720)], "azure/gpt-4.1") == 85 + 170 * 6


def test_gpt5_mini_uses_patch_tokenization() -> None:
    # gpt-5-mini is patch-based (32px patches, 1536 cap, x1.62) — NOT legacy tile math
    tile = estimate_image_tokens([_png(1920, 1080)], "gpt-4o")  # 1105
    patch = estimate_image_tokens([_png(1920, 1080)], "azure/skyvern-applications-gpt-5-mini")
    assert patch > 2 * tile  # ~2443, ~2.2x the tile undercount
    assert 2400 <= patch <= 2500
    # under the 1536-patch budget no scaling happens: 10x10 patches x 1.62
    assert estimate_image_tokens([_png(320, 320)], "gpt-5-mini") == round(100 * 1.62)


def test_patch_multiplier_varies_by_model_class() -> None:
    small = [_png(320, 320)]  # 100 patches, under budget, no scaling
    assert estimate_image_tokens(small, "gpt-5-mini") == round(100 * 1.62)
    assert estimate_image_tokens(small, "gpt-5-nano") == round(100 * 2.46)
    assert estimate_image_tokens(small, "o4-mini") == round(100 * 1.72)
    assert estimate_image_tokens(small, "gpt-4.1-mini") == round(100 * 1.62)


def test_anthropic_caps_large_images() -> None:
    # 1920x1080 -> 2_073_600 / 750 = 2765 -> capped at 1600
    assert estimate_image_tokens([_png(1920, 1080)], "bedrock/us.anthropic.claude-sonnet-4-6") == 1600
    # small image stays under the cap
    assert estimate_image_tokens([_png(600, 600)], "claude-sonnet-4-6") == round(600 * 600 / 750)


def test_multiple_screenshots_sum() -> None:
    one = estimate_image_tokens([_png(1280, 720)], "gemini-2.5-flash")
    three = estimate_image_tokens([_png(1280, 720)] * 3, "gemini-2.5-flash")
    assert three == 3 * one


def test_cost_is_zero_without_tokens_or_price() -> None:
    assert estimate_image_cost(0, "gpt-5-mini") == 0.0
    assert estimate_image_cost(1000, "totally-unknown-model-xyz") == 0.0


def test_provider_image_tokens_reads_billed_value() -> None:
    assert provider_image_tokens(_response(1234)) == 1234
    assert provider_image_tokens(_response("1500")) == 1500


def test_provider_image_tokens_returns_none_when_absent() -> None:
    # OpenAI/Anthropic leave image_tokens unset
    assert provider_image_tokens(_response(None)) is None
    # malformed / missing pieces
    assert provider_image_tokens(_response("not-an-int")) is None
    assert provider_image_tokens(_response(-5)) is None
    assert provider_image_tokens(SimpleNamespace(usage=None)) is None
    assert provider_image_tokens(SimpleNamespace()) is None
