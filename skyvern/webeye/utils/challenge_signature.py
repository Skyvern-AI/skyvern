"""Challenge vendor signature and on-screen challenge-frame probe. The signature is also spliced into a JavaScript
regex literal, so it must stay in syntax both engines share: no named groups, no lookbehind, no ``/``."""

from __future__ import annotations

import asyncio
import re
from enum import StrEnum

from playwright.async_api import Frame

CHALLENGE_VENDOR_SIGNATURE = (
    r"captcha|turnstile|challenges\.cloudflare|arkoselabs|funcaptcha|datadome|perimeterx"
    r"|verify you are human|security challenge"
)
CHALLENGE_VENDOR_FRAME_URL = re.compile(CHALLENGE_VENDOR_SIGNATURE, re.IGNORECASE)


class ChallengeVendor(StrEnum):
    """A bot-defense vendor whose challenge page a deployment can hand to its own handler."""

    DATADOME = "datadome"
    PERIMETERX = "perimeterx"


# A managed widget preloads small (a bordered 1x1 iframe measures 25) and grows when it challenges, so an area at or
# under this is a placeholder. ponytail: one sentinel for every vendor; revisit if a real widget ships under 16x16.
CHALLENGE_FRAME_PLACEHOLDER_AREA = 256.0

# A full-size preload can be off-viewport, clipped, or hidden by style and still report its whole box.
# checkVisibility answers style and IntersectionObserver answers geometry, ancestor clip rects included.
_CHALLENGE_FRAME_ONSCREEN_AREA_JS = """
el => {
  if (el.checkVisibility && !el.checkVisibility({
    opacityProperty: true, visibilityProperty: true, contentVisibilityAuto: true,
  })) return 0;
  return new Promise(resolve => {
    let observer = null;
    let timer = null;
    const done = area => {
      if (observer) observer.disconnect();
      if (timer) clearTimeout(timer);
      resolve(area);
    };
    observer = new IntersectionObserver(entries => {
      const rect = entries[entries.length - 1].intersectionRect;
      done(rect.width * rect.height);
    });
    timer = setTimeout(() => done(null), 1000);
    observer.observe(el);
  });
}
"""


_ELEMENT_STYLE_VISIBLE_JS = """
el => !el.checkVisibility || el.checkVisibility({
  opacityProperty: true, visibilityProperty: true, contentVisibilityAuto: true,
})
"""


async def _frame_element_style_visible(frame: Frame) -> bool | None:
    try:
        element = await frame.frame_element()
        return bool(await element.evaluate(_ELEMENT_STYLE_VISIBLE_JS))
    except Exception:
        return None


async def _embedding_frames_style_visible(frame: Frame) -> bool | None:
    """Whether every iframe embedding this one is visible by style, since style is judged per document and a hidden
    ancestor iframe still lets this frame report its full box; None when none reads as hidden but one could not be
    read."""
    ancestors: list[Frame] = []
    current = frame.parent_frame
    while current is not None and current.parent_frame is not None:
        ancestors.append(current)
        current = current.parent_frame
    visible = await asyncio.gather(*(_frame_element_style_visible(ancestor) for ancestor in ancestors))
    if False in visible:
        return False
    return None if None in visible else True


# The script's own timer cannot fire in a renderer that never yields, so the deadline is held here.
_CHALLENGE_FRAME_PROBE_TIMEOUT_SECONDS = 2.0


async def challenge_frame_rendered_area(frame: Frame) -> float | None:
    """On-screen area of the frame's own element, or None when it cannot be measured in time."""
    try:
        return await asyncio.wait_for(
            _measure_challenge_frame_area(frame), timeout=_CHALLENGE_FRAME_PROBE_TIMEOUT_SECONDS
        )
    except Exception:
        return None


async def _measure_challenge_frame_area(frame: Frame) -> float | None:
    element = await frame.frame_element()
    area = await element.evaluate(_CHALLENGE_FRAME_ONSCREEN_AREA_JS)
    if area is None:
        return None
    embedding_visible = await _embedding_frames_style_visible(frame)
    if embedding_visible is None:
        return None
    return float(area) if embedding_visible else 0.0


async def rendered_challenge_vendor(frames: list[Frame]) -> str | None:
    """Vendor of the first of these frames that is a challenge frame rendered on screen above placeholder size."""
    vendor, _unmeasured = await rendered_challenge_vendor_reading(frames)
    return vendor


async def rendered_challenge_vendor_reading(frames: list[Frame]) -> tuple[str | None, bool]:
    """The rendered vendor, and whether a vendor frame went unmeasured when none measured as rendered."""
    # A captured frame can navigate away before this runs, so each is matched on its current URL.
    candidates = [
        (frame, match) for frame in frames if (match := CHALLENGE_VENDOR_FRAME_URL.search(frame.url or "")) is not None
    ]
    areas = await asyncio.gather(*(challenge_frame_rendered_area(frame) for frame, _match in candidates))
    for (_frame, match), area in zip(candidates, areas, strict=True):
        if area is not None and area > CHALLENGE_FRAME_PLACEHOLDER_AREA:
            # The vendor is named by the signature literal that matched, never by the hostname, which can carry
            # a tenant slug or a secret in a spelling no scrub can enumerate.
            return match.group(0).casefold(), False
    return None, any(area is None for area in areas)
