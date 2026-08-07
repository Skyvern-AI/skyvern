export function mouseButtonName(button: number): string {
  if (button === 2) return "right";
  if (button === 1) return "middle";
  return "left";
}

// CDP's Input.dispatchKeyEvent only performs the browser's default edit action (delete
// forward/backward, arrow-key caret movement, etc.) for non-printable keys when given a
// windowsVirtualKeyCode alongside eventType "rawKeyDown" - key/code alone are a no-op.
const VIRTUAL_KEY_CODES: Record<string, number> = {
  Backspace: 8,
  Tab: 9,
  Enter: 13,
  Escape: 27,
  PageUp: 33,
  PageDown: 34,
  End: 35,
  Home: 36,
  ArrowLeft: 37,
  ArrowUp: 38,
  ArrowRight: 39,
  ArrowDown: 40,
  Insert: 45,
  Delete: 46,
  F1: 112,
  F2: 113,
  F3: 114,
  F4: 115,
  F5: 116,
  F6: 117,
  F7: 118,
  F8: 119,
  F9: 120,
  F10: 121,
  F11: 122,
  F12: 123,
};

export function virtualKeyCodeFor(
  e: Pick<KeyboardEvent, "key" | "code">,
): number | undefined {
  if (e.key === " ") {
    return 32;
  }
  return VIRTUAL_KEY_CODES[e.key] ?? VIRTUAL_KEY_CODES[e.code];
}

export function getModifiers(
  e: Pick<KeyboardEvent, "altKey" | "ctrlKey" | "metaKey" | "shiftKey">,
): number {
  let m = 0;
  if (e.altKey) m |= 1;
  if (e.ctrlKey) m |= 2;
  if (e.metaKey) m |= 4;
  if (e.shiftKey) m |= 8;
  return m;
}

/**
 * Map pixel coordinates from a rendered image back to viewport coordinates,
 * accounting for object-contain letterboxing.
 */
export function mapCoordinates(
  clientX: number,
  clientY: number,
  rect: DOMRect,
  vpW: number,
  vpH: number,
): { x: number; y: number } | null {
  const containerAspect = rect.width / rect.height;
  const imageAspect = vpW / vpH;

  let renderedW: number, renderedH: number, offsetX: number, offsetY: number;
  if (containerAspect > imageAspect) {
    renderedH = rect.height;
    renderedW = rect.height * imageAspect;
    offsetX = (rect.width - renderedW) / 2;
    offsetY = 0;
  } else {
    renderedW = rect.width;
    renderedH = rect.width / imageAspect;
    offsetX = 0;
    offsetY = (rect.height - renderedH) / 2;
  }

  const localX = clientX - rect.left - offsetX;
  const localY = clientY - rect.top - offsetY;

  if (localX < 0 || localX > renderedW || localY < 0 || localY > renderedH) {
    return null;
  }

  return {
    x: Math.round(localX * (vpW / renderedW)),
    y: Math.round(localY * (vpH / renderedH)),
  };
}

/**
 * Convenience wrapper for React MouseEvent on an img element.
 */
export function mapMouseCoordinates(
  e: React.MouseEvent<HTMLImageElement>,
  vpW: number,
  vpH: number,
): { x: number; y: number } | null {
  return mapCoordinates(
    e.clientX,
    e.clientY,
    e.currentTarget.getBoundingClientRect(),
    vpW,
    vpH,
  );
}
