// Width of the docked Copilot spine (px), and its collapsed-rail width.
export const STUDIO_COPILOT_WIDTH = 450;
export const STUDIO_COPILOT_RAIL_WIDTH = 60;

// Stable element ids linking each tab to its panel (WAI-ARIA tabs pattern).
export const studioTabId = (tab: string) => `studio-tab-${tab}`;
export const studioPanelId = (tab: string) => `studio-panel-${tab}`;
