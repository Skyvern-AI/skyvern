/**
 * Hairline between the workflow title and layout controls in the studio top
 * bar. Pane headers use spacing instead, so they do not repeat this boundary.
 */
export function PaneHeaderDivider() {
  return <span aria-hidden className="h-[18px] w-px shrink-0 bg-border" />;
}
