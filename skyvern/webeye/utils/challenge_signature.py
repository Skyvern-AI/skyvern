"""Challenge vendor signature and on-screen challenge-frame probe. The signature is also spliced into a JavaScript
regex literal, so it must stay in syntax both engines share: no named groups, no lookbehind, no ``/``."""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass
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
    BOTSHIELD = "botshield"


# A managed widget preloads small (a bordered 1x1 iframe measures 25) and grows when it challenges, so an area at or
# under this is a placeholder. ponytail: one sentinel for every vendor; revisit if a real widget ships under 16x16.
CHALLENGE_FRAME_PLACEHOLDER_AREA = 256.0

# A full-size preload can be off-viewport, clipped, or hidden by style and still report its whole box.
# checkVisibility answers style and IntersectionObserver answers geometry, ancestor clip rects included.
_CHALLENGE_FRAME_ONSCREEN_SIZE_JS = """
el => {
  if (el.checkVisibility && !el.checkVisibility({
    opacityProperty: true, visibilityProperty: true, contentVisibilityAuto: true,
  })) return [0, 0];
  return new Promise(resolve => {
    let observer = null;
    let timer = null;
    const done = size => {
      if (observer) observer.disconnect();
      if (timer) clearTimeout(timer);
      resolve(size);
    };
    observer = new IntersectionObserver(entries => {
      const rect = entries[entries.length - 1].intersectionRect;
      done([rect.width, rect.height]);
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


async def challenge_frame_rendered_size(frame: Frame) -> tuple[float, float] | None:
    """On-screen width and height of the frame's own element, or None when it cannot be measured in time."""
    try:
        return await asyncio.wait_for(
            _measure_challenge_frame_size(frame), timeout=_CHALLENGE_FRAME_PROBE_TIMEOUT_SECONDS
        )
    except Exception:
        return None


async def challenge_frame_rendered_area(frame: Frame) -> float | None:
    size = await challenge_frame_rendered_size(frame)
    return None if size is None else size[0] * size[1]


async def _measure_challenge_frame_size(frame: Frame) -> tuple[float, float] | None:
    element = await frame.frame_element()
    size = await element.evaluate(_CHALLENGE_FRAME_ONSCREEN_SIZE_JS)
    if size is None:
        return None
    embedding_visible = await _embedding_frames_style_visible(frame)
    if embedding_visible is None:
        return None
    width, height = float(size[0]), float(size[1])
    # The observer runs in the page's own JavaScript world, so a page that replaces it can report any number.
    if not (math.isfinite(width) and math.isfinite(height)):
        return None
    return (width, height) if embedding_visible else (0.0, 0.0)


@dataclass(frozen=True)
class SolverExtensionCheck:
    """What a deployment's solver extension found on the page. ``label`` names what it found, or what it looked for
    when it found nothing; ``unfinished`` means it found nothing but one of its looks did not finish."""

    found: bool
    solved: bool
    label: str | None = None
    unfinished: bool = False


@dataclass(frozen=True)
class ChallengeFrameReading:
    """Vendor-named child frames measured on screen above placeholder size, as "'<literal>' WxH px", and how many
    vendor-named frames could not be measured. ``vendor`` is the first rendered frame's literal."""

    vendor: str | None
    rendered: tuple[str, ...]
    unmeasured: int

    def rendered_sentence(self) -> str:
        if self.rendered:
            more = f" and {len(self.rendered) - 3} more" if len(self.rendered) > 3 else ""
            return "Frames on screen whose address names a captcha vendor: " + ", ".join(self.rendered[:3]) + more + "."
        return "" if self.unmeasured else "Frames on screen whose address names a captcha vendor: none."

    def unmeasured_phrase(self) -> str:
        noun = "frame" if self.unmeasured == 1 else "frames"
        return f"{self.unmeasured} {noun} whose address names a captcha vendor could not be measured"

    def frames_sentence(self) -> str:
        sentences = [self.rendered_sentence()]
        if self.unmeasured:
            sentences.append(self.unmeasured_phrase() + ".")
        return " ".join(sentence for sentence in sentences if sentence)


async def challenge_frame_reading(frames: list[Frame]) -> ChallengeFrameReading:
    # A captured frame can navigate away before this runs, so each is matched on its current URL.
    candidates = [
        (frame, match) for frame in frames if (match := CHALLENGE_VENDOR_FRAME_URL.search(frame.url or "")) is not None
    ]
    sizes = await asyncio.gather(*(challenge_frame_rendered_size(frame) for frame, _match in candidates))
    # The vendor is named by the signature literal that matched, never by the hostname, which can carry a tenant
    # slug or a secret in a spelling no scrub can enumerate.
    shown = [
        (match.group(0).casefold(), size)
        for (_frame, match), size in zip(candidates, sizes, strict=True)
        if size is not None and size[0] * size[1] > CHALLENGE_FRAME_PLACEHOLDER_AREA
    ]
    return ChallengeFrameReading(
        vendor=shown[0][0] if shown else None,
        rendered=tuple(f"'{literal}' {round(width)}x{round(height)} px" for literal, (width, height) in shown),
        unmeasured=sum(size is None for size in sizes),
    )
