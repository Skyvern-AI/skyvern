export function mouseButtonName(button: number): string {
  if (button === 2) return "right";
  if (button === 1) return "middle";
  return "left";
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

const WINDOWS_VIRTUAL_KEY_CODES: Record<string, number> = {
  Backspace: 8,
  Tab: 9,
  Enter: 13,
  Shift: 16,
  Control: 17,
  Alt: 18,
  Pause: 19,
  CapsLock: 20,
  Escape: 27,
  " ": 32,
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
  Meta: 91,
  ContextMenu: 93,
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
  NumLock: 144,
  ScrollLock: 145,
};

export type CdpKeyEventPayload = {
  type: "keyEvent";
  eventType: "keyDown" | "keyUp" | "rawKeyDown";
  key: string;
  code: string;
  modifiers: number;
  text?: string;
  windowsVirtualKeyCode?: number;
};

type KeyboardEventLike = Pick<
  KeyboardEvent,
  "altKey" | "code" | "ctrlKey" | "key" | "metaKey" | "shiftKey"
>;

function windowsVirtualKeyCodeForKey(key: string): number | undefined {
  if (/^[a-z]$/i.test(key)) {
    return key.toUpperCase().charCodeAt(0);
  }
  if (/^[0-9]$/.test(key)) {
    return key.charCodeAt(0);
  }
  return WINDOWS_VIRTUAL_KEY_CODES[key];
}

export function buildKeyDownPayload(e: KeyboardEventLike): CdpKeyEventPayload {
  const windowsVirtualKeyCode = windowsVirtualKeyCodeForKey(e.key);
  const isPrintableSingleCharacter = e.key.length === 1;
  const eventType =
    !isPrintableSingleCharacter && windowsVirtualKeyCode !== undefined
      ? "rawKeyDown"
      : "keyDown";

  return {
    type: "keyEvent",
    eventType,
    key: e.key,
    code: e.code,
    ...(isPrintableSingleCharacter ? { text: e.key } : {}),
    ...(windowsVirtualKeyCode !== undefined ? { windowsVirtualKeyCode } : {}),
    modifiers: getModifiers(e),
  };
}

export function buildKeyUpPayload(e: KeyboardEventLike): CdpKeyEventPayload {
  const windowsVirtualKeyCode = windowsVirtualKeyCodeForKey(e.key);

  return {
    type: "keyEvent",
    eventType: "keyUp",
    key: e.key,
    code: e.code,
    ...(windowsVirtualKeyCode !== undefined ? { windowsVirtualKeyCode } : {}),
    modifiers: getModifiers(e),
  };
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
