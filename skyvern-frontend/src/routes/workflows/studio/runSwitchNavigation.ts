import { useCallback } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { useRunViewStore } from "@/store/RunViewStore";

import { liveSearch } from "./liveSearch";
import { toReadableSearch } from "./panes";

// Point the studio at a different run: set ?wr=, drop the per-run selection
// params (?active=, ?bl=, ?iteration=), and keep everything else — notably
// ?panes=, so the layout rides through untouched. The caller merges this
// against the LIVE URL string, never a render-closure (a concurrent navigate is
// already visible there), same rule as useStudioPanes.
export function searchWithRunSwitched(search: string, runId: string): string {
  const params = new URLSearchParams(search);
  params.set("wr", runId);
  params.delete("active");
  params.delete("bl");
  // A loop-iteration scope belongs to the run being left (WorkflowRun.tsx
  // forwards it into studio URLs); inert in studio today, but clearing it keeps
  // this the single, complete home for run-switch navigation.
  params.delete("iteration");
  return toReadableSearch(params);
}

/**
 * Switch the studio's inspected run from a user action (e.g. the Past Runs
 * list). The single place run-switch navigation lives, so surfaces that touch
 * it stay consistent. A pinned frame belongs to the run being left, so it is
 * dropped before the switch; RunView re-resolves the new run's selection.
 */
export function useSwitchStudioRun(): (runId: string) => void {
  const navigate = useNavigate();
  const location = useLocation();
  return useCallback(
    (runId: string) => {
      useRunViewStore.getState().reset();
      // Push, not replace (unlike the pane-toggle writes in useStudioPanes): a
      // run switch is a real user navigation, so browser back/forward steps
      // through the runs the user has viewed.
      navigate({
        search: searchWithRunSwitched(liveSearch(location.search), runId),
      });
    },
    [navigate, location.search],
  );
}
