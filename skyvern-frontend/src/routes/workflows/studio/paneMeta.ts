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
  // "Overview" is retired from display (paneLabel returns "Run" / "Run: wr_…");
  // it stays here as the registry fallback name for the pane.
  overview: { label: "Overview", icon: ReaderIcon },
};

// Head-truncate a run id for the run pane's tab label, e.g. "wr_5538…".
function truncateRunId(runId: string): string {
  return runId.length > 7 ? `${runId.slice(0, 7)}…` : runId;
}

// The run pane ("overview") shows the inspected run instead of a static name:
// "Run: wr_5538…" while a run is inspected, "Run" when none. Every other pane
// keeps its registry label. Callers pass the URL-addressed run id
// (useStudioRunId) — deliberately NOT the latest-run fallback — so the label
// identifies the run named in the shareable URL, not just whatever is on screen
// (RunsTab's highlight, by contrast, follows the fallback via useStudioInspectedRun).
export function paneLabel(id: StudioPaneId, runId?: string | null): string {
  if (id === "overview") {
    return runId ? `Run: ${truncateRunId(runId)}` : "Run";
  }
  return STUDIO_PANE_META[id].label;
}

// The stable accessible name for a pane's OWN controls (region, header, drag,
// close, resize) and announcements. The run pane keeps "Run" so a screen reader
// hears "Close Run pane", matching its "Run: wr_…" content — "Past Runs" is the
// selector trigger's name (railLabel), not the pane's. The run id never enters
// this name: a truncated id is ambiguous and would rename the control on every
// run switch.
export function paneAccessibleName(id: StudioPaneId): string {
  return paneLabel(id);
}

// The rail tab / stage-launcher tile label. The run pane's tab is the "Past
// Runs" selector (it opens the run history rather than toggling a pane), so it
// reads "Past Runs"; every other tab matches its pane's accessible name.
export function railLabel(id: StudioPaneId): string {
  return id === "overview" ? "Past Runs" : paneAccessibleName(id);
}
