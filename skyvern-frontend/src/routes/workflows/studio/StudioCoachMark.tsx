import { useState } from "react";
import { useLocation } from "react-router-dom";

import { Button } from "@/components/ui/button";
import { useStudioRunRouteMatch } from "@/routes/workflows/useStudioRunRouteMatch";
import { useStudioFirstRunStore } from "@/store/StudioFirstRunStore";

import { liveSearch } from "./liveSearch";

/**
 * One-time callout under the top bar's pane toggles teaching that they open
 * side-by-side panes. Suppressed on deep-linked visits (a shared run link is
 * the wrong moment to teach layout); the first pane toggle also dismisses it.
 */
export function StudioCoachMark() {
  const coachMarkSeen = useStudioFirstRunStore((s) => s.coachMarkSeen);
  const markCoachMarkSeen = useStudioFirstRunStore((s) => s.markCoachMarkSeen);
  const location = useLocation();
  const runRouteMatch = useStudioRunRouteMatch();
  const [deepLinked] = useState(() => {
    const params = new URLSearchParams(liveSearch(location.search));
    return Boolean(
      params.get("wr") ||
      params.get("active") ||
      params.get("bl") ||
      runRouteMatch,
    );
  });

  if (coachMarkSeen || deepLinked) {
    return null;
  }

  return (
    <div
      role="note"
      aria-label="Studio panes tip"
      className="absolute left-1/2 top-3 z-50 w-64 -translate-x-1/2 rounded-md border bg-popover p-3 text-popover-foreground shadow-md duration-200 motion-safe:animate-in motion-safe:fade-in motion-safe:slide-in-from-top-2"
    >
      <span
        aria-hidden
        className="absolute -top-1 left-1/2 size-2 -translate-x-1/2 rotate-45 border-l border-t bg-popover"
      />
      <p className="text-xs font-medium">Panes open side by side</p>
      <p className="mt-1 text-xs text-muted-foreground">
        Each toggle in the top bar opens a pane, so you can watch the browser
        while you edit, or keep Copilot next to a run.
      </p>
      <Button
        variant="secondary"
        size="sm"
        className="mt-2 h-7 px-2 text-xs"
        onClick={markCoachMarkSeen}
      >
        Got it
      </Button>
    </div>
  );
}
