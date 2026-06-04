from io import BytesIO

from PIL import Image

from skyvern.utils.image_resizer import downscale_screenshots_to_height


def _png(width: int, height: int) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="PNG")
    return buf.getvalue()


def _dims(screenshot: bytes) -> tuple[int, int]:
    return Image.open(BytesIO(screenshot)).size


def test_downscales_1080p_to_768p_preserving_aspect() -> None:
    out = downscale_screenshots_to_height([_png(1920, 1080)], 768)
    assert _dims(out[0]) == (1365, 768)


def test_leaves_images_at_or_below_target_unchanged() -> None:
    original = _png(1280, 720)
    out = downscale_screenshots_to_height([original], 720)
    assert out[0] is original  # no re-encode: avoids a needless PNG round-trip + byte bloat
    assert _dims(downscale_screenshots_to_height([_png(800, 600)], 720)[0]) == (800, 600)


def test_disabled_when_max_height_non_positive() -> None:
    shots = [_png(1920, 1080)]
    assert downscale_screenshots_to_height(shots, 0) is shots


def test_non_16_9_aspect_preserved() -> None:
    # 1000x1000 -> height 500, width scales proportionally
    assert _dims(downscale_screenshots_to_height([_png(1000, 1000)], 500)[0]) == (500, 500)


def test_corrupt_screenshot_kept_and_batch_continues() -> None:
    good = _png(1920, 1080)
    corrupt = b"not-a-png"
    out = downscale_screenshots_to_height([good, corrupt], 768)
    assert _dims(out[0]) == (1365, 768)  # good one still downscaled
    assert out[1] is corrupt  # bad one kept as-is, batch not aborted
