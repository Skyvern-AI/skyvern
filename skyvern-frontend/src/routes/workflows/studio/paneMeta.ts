import { type ComponentType } from "react";
import {
  ChatBubbleIcon,
  GlobeIcon,
  ReaderIcon,
  Share1Icon,
} from "@radix-ui/react-icons";

import { type StudioPaneId } from "./panes";

export const STUDIO_PANE_META: Record<
  StudioPaneId,
  { label: string; icon: ComponentType<{ className?: string }> }
> = {
  copilot: { label: "Copilot", icon: ChatBubbleIcon },
  editor: { label: "Editor", icon: Share1Icon },
  browser: { label: "Browser", icon: GlobeIcon },
  // The internal "overview" pane is displayed as "Run" in Studio.
  overview: { label: "Overview", icon: ReaderIcon },
};

export function paneLabel(id: StudioPaneId): string {
  return id === "overview" ? "Run" : STUDIO_PANE_META[id].label;
}

// Stable names keep the run pane's region and controls as "Run" across run switches.
export function paneAccessibleName(id: StudioPaneId): string {
  return paneLabel(id);
}

// The rail control / stage-launcher tile label. The inspected run's primary
// control uses its FULL id ("View Run: wr_55380…" — the top bar is where people
// read and copy run ids, so no truncation), while empty-state surfaces that
// cannot open a run yet read "Past Runs". Every other control matches its
// pane's accessible name.
export function railLabel(id: StudioPaneId, runId?: string | null): string {
  if (id === "overview") {
    return runId ? `View Run: ${runId}` : "Past Runs";
  }
  return paneAccessibleName(id);
}
