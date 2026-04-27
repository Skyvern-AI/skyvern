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
