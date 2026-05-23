// Shared visual treatment for "this canvas element is the active selection."
// Used by withSelectableBlock for workflow-block tiles and by NodeAdderNode
// for the + CTA when its add session is open. Pair with `transition-shadow`
// on the wrapper so the ring fades in/out when selection state flips.
export const SELECTED_RING_CLASSES =
  "shadow-[0_0_16px_rgba(59,130,246,0.5)] ring-2 ring-blue-500 ring-offset-2 ring-offset-slate-950";
