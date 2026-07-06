// Duration (ms) of studio column-width transitions; the editor's viewport
// counter-translate tracks this window (see FlowRenderer).
export const STUDIO_COPILOT_TRANSITION_MS = 300;

// Stable element ids linking each top-bar toggle to the pane it controls.
export const studioTabId = (tab: string) => `studio-tab-${tab}`;
export const studioPanelId = (tab: string) => `studio-panel-${tab}`;

// Bordered icon-square pane-header action per the studio button grammar
// (cloud_docs/frontend/studio-button-system.md).
export const PANE_HEADER_ICON_BUTTON_CLASS =
  "inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-border text-muted-foreground hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-40";
