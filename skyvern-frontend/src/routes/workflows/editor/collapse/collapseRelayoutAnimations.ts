// Height-animating collapse/accordion keyframes (must match tailwind.config).
// When one ends, the node has settled and the canvas must re-stack below it.
export const HEIGHT_COLLAPSE_ANIMATION_NAMES: ReadonlySet<string> = new Set([
  "accordion-down",
  "accordion-up",
  "collapsible-down",
  "collapsible-up",
  "collapsible-down-fade",
  "collapsible-up-fade",
]);

export function isHeightCollapseAnimation(animationName: string): boolean {
  return HEIGHT_COLLAPSE_ANIMATION_NAMES.has(animationName);
}
