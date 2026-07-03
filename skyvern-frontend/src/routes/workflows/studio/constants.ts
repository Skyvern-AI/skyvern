// Duration (ms) of studio column-width transitions; the editor's viewport
// counter-translate tracks this window (see FlowRenderer).
export const STUDIO_COPILOT_TRANSITION_MS = 300;

// Stable element ids linking each spine tab to the pane it toggles.
export const studioTabId = (tab: string) => `studio-tab-${tab}`;
export const studioPanelId = (tab: string) => `studio-panel-${tab}`;
