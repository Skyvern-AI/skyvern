import { type StudioTab } from "@/store/StudioShellStore";

// Width of the docked Copilot spine (px), and its collapsed-rail width.
export const STUDIO_COPILOT_WIDTH = 450;
export const STUDIO_COPILOT_RAIL_WIDTH = 60;

// Duration (ms) of the Copilot column open/collapse transition.
export const STUDIO_COPILOT_TRANSITION_MS = 300;
// Open uses an ease-out (quick reveal that settles). Collapse uses a symmetric
// ease-in-out: the ease-out is front-loaded, so on close it empties in the first
// ~third and reads as faster than the open — the even curve keeps them matched.
export const STUDIO_COPILOT_TRANSITION_EASE = "cubic-bezier(0.22, 1, 0.36, 1)";
export const STUDIO_COPILOT_COLLAPSE_EASE = "cubic-bezier(0.65, 0, 0.35, 1)";

// Stable element ids linking each tab to its panel (WAI-ARIA tabs pattern).
export const studioTabId = (tab: string) => `studio-tab-${tab}`;
export const studioPanelId = (tab: string) => `studio-panel-${tab}`;

// Deep-link landing tab: any run reference (?wr=) or pinned item (?active=) → Run; else Editor.
export function initialStudioTab(params: {
  runId: string | null;
  active: string | null;
}): StudioTab {
  return params.runId || params.active ? "run" : "editor";
}
