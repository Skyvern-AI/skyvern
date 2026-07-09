/**
 * Hairline between a title and its tab cluster (top bar and pane headers) —
 * part of the studio button grammar (tabs are borderless ghost pills; the
 * divider carries the visual separation the old pill containers provided).
 */
export function PaneHeaderDivider() {
  return <span aria-hidden className="h-[18px] w-px shrink-0 bg-border" />;
}
