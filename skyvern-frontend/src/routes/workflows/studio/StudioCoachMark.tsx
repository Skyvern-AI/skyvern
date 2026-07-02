import { useState } from "react";
import { useLocation } from "react-router-dom";

import { Button } from "@/components/ui/button";
import { useStudioFirstRunStore } from "@/store/StudioFirstRunStore";

import { liveSearch } from "./liveSearch";

/**
 * One-time callout beside the spine teaching that tabs open side-by-side
 * panes. Suppressed on deep-linked visits (a shared run link is the wrong
 * moment to teach layout); the first pane toggle also dismisses it for good.
 */
export function StudioCoachMark() {
  const coachMarkSeen = useStudioFirstRunStore((s) => s.coachMarkSeen);
  const markCoachMarkSeen = useStudioFirstRunStore((s) => s.markCoachMarkSeen);
  const location = useLocation();
  const [deepLinked] = useState(() => {
    const params = new URLSearchParams(liveSearch(location.search));
    return Boolean(
      params.get("wr") || params.get("active") || params.get("bl"),
    );
  });

  if (coachMarkSeen || deepLinked) {
    return null;
  }

  return (
    <div
      role="note"
      aria-label="Studio panes tip"
      className="absolute left-3 top-8 z-50 w-64 rounded-md border bg-popover p-3 text-popover-foreground shadow-md duration-200 motion-safe:animate-in motion-safe:fade-in motion-safe:slide-in-from-left-2"
    >
      <span
        aria-hidden
        className="absolute -left-1 top-5 size-2 rotate-45 border-b border-l bg-popover"
      />
      <p className="text-xs font-medium">Panes open side by side</p>
      <p className="mt-1 text-xs text-muted-foreground">
        Each tab on this rail toggles a pane, so you can watch the browser while
        you edit, or keep Copilot next to a run.
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
