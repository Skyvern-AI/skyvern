const MIRROR_STYLES = [
  "direction",
  "boxSizing",
  "width",
  "height",
  "overflowX",
  "overflowY",
  "borderTopWidth",
  "borderRightWidth",
  "borderBottomWidth",
  "borderLeftWidth",
  "borderStyle",
  "paddingTop",
  "paddingRight",
  "paddingBottom",
  "paddingLeft",
  "fontStyle",
  "fontVariant",
  "fontWeight",
  "fontStretch",
  "fontSize",
  "fontSizeAdjust",
  "lineHeight",
  "fontFamily",
  "textAlign",
  "textTransform",
  "textIndent",
  "textDecoration",
  "letterSpacing",
  "wordSpacing",
  "tabSize",
  "MozTabSize",
  "whiteSpace",
  "wordWrap",
  "wordBreak",
] as const;

type CaretCoordinates = {
  top: number;
  left: number;
};

/**
 * Compute pixel coordinates of the caret at `position` inside a textarea
 * using the "mirror div" technique.
 */
function getTextareaCaretCoordinates(
  element: HTMLTextAreaElement,
  position: number,
): CaretCoordinates {
  const mirror = document.createElement("div");
  mirror.id = "caret-mirror";

  document.body.appendChild(mirror);

  const style = mirror.style;
  const computed = window.getComputedStyle(element);

  style.position = "absolute";
  style.visibility = "hidden";
  style.whiteSpace = "pre-wrap";
  style.wordWrap = "break-word";
  style.overflow = "hidden";

  for (const prop of MIRROR_STYLES) {
    // Use bracket notation — camelCase works with style[prop] but not
    // setProperty/getPropertyValue (which require kebab-case).
    (style as unknown as Record<string, string>)[prop] =
      (computed as unknown as Record<string, string>)[prop] ?? "";
  }

  // Copy the text up to the caret position
  mirror.textContent = element.value.substring(0, position);

  // Add a span at the caret position to measure its offset
  const span = document.createElement("span");
  span.textContent = element.value.substring(position) || ".";
  mirror.appendChild(span);

  const coordinates: CaretCoordinates = {
    top: span.offsetTop - element.scrollTop,
    left: span.offsetLeft - element.scrollLeft,
  };

  document.body.removeChild(mirror);

  return coordinates;
}

export { getTextareaCaretCoordinates };
export type { CaretCoordinates };
