// Duration (ms) of studio column-width transitions; the editor's viewport
// counter-translate tracks this window (see FlowRenderer).
export const STUDIO_COPILOT_TRANSITION_MS = 300;

// Stable element ids linking each top-bar toggle to the pane it controls.
export const studioTabId = (tab: string) => `studio-tab-${tab}`;
export const studioPanelId = (tab: string) => `studio-panel-${tab}`;

// Ghost icon-square pane-header action per the studio button grammar
// (cloud_docs/frontend/studio-button-system.md).
export const PANE_HEADER_ICON_BUTTON_CLASS =
  "inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-40";

// Same ghost icon-square footprint as PANE_HEADER_ICON_BUTTON_CLASS, with the
// destructive color on the icon for the one control that actually ends the
// live browser session (documented exception in studio-button-system.md,
// SKY-12247) — do not reuse for Reconnect/Open-in-new-tab/pane-close, which
// don't carry that consequence.
export const PANE_HEADER_ICON_BUTTON_DESTRUCTIVE_CLASS =
  "inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-destructive hover:bg-destructive/10 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-40";
