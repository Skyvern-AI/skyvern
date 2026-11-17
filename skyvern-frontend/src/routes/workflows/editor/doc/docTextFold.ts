export const DOC_COLLAPSED_LINES = 3;

// Half a line of slack, so a reference pill's padding never counts as another line.
export function docTextOverflows(el: HTMLElement): boolean {
  const style = getComputedStyle(el);
  const lineHeight =
    parseFloat(style.lineHeight) || (parseFloat(style.fontSize) || 16) * 1.55;
  return el.scrollHeight > lineHeight * (DOC_COLLAPSED_LINES + 0.5);
}
