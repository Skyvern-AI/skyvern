import { useCallback } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { useRunViewStore } from "@/store/RunViewStore";

import { liveSearch } from "./liveSearch";
import {
  searchWithRunReference,
  SYSTEM_RUN_FOCUS_PARAM,
  toReadableSearch,
} from "./panes";
import { useStudioRunId } from "./useStudioRunId";

// Point the studio at a different run: set ?wr=, drop the per-run selection
// params (?active=, ?bl=, ?iteration=), and keep everything else — notably
// ?panes=, so the layout rides through untouched. The caller merges this
// against the LIVE URL string, never a render-closure (a concurrent navigate is
// already visible there), same rule as useStudioPanes.
export function searchWithRunSwitched(
  search: string,
  runId: string,
  options?: { systemFocus?: boolean },
): string {
  const params = new URLSearchParams(search);
  // The marker says the copilot INTRODUCED this run reference, so the layout
  // class is whatever it would have been without the copilot. Marking a run
  // the user already opened would flip them out of the run class they chose —
  // the same remap this marker exists to prevent.
  const copilotOwnsFocus =
    params.get(SYSTEM_RUN_FOCUS_PARAM) !== null ||
    (params.get("wr") === null && params.get("active") === null);
  params.set("wr", runId);
  if (options?.systemFocus && copilotOwnsFocus) {
    params.set(SYSTEM_RUN_FOCUS_PARAM, "copilot");
  } else {
    // A user switching runs takes over the focus; the layout reclassifies.
    params.delete(SYSTEM_RUN_FOCUS_PARAM);
  }
  params.delete("active");
  params.delete("bl");
  // A loop-iteration scope belongs to the run being left (WorkflowRun.tsx
  // forwards it into studio URLs); inert in studio today, but clearing it keeps
  // this the single, complete home for run-switch navigation.
  params.delete("iteration");
  return toReadableSearch(params);
}

// The inverse of searchWithRunSwitched: stop inspecting any run, keeping the
// rest of the URL (notably ?panes=) untouched.
export function searchWithRunCleared(search: string): string {
  const params = new URLSearchParams(search);
  params.delete("wr");
  params.delete(SYSTEM_RUN_FOCUS_PARAM);
  params.delete("active");
  params.delete("bl");
  params.delete("iteration");
  return toReadableSearch(params);
}

/**
 * Release a run the caller focused itself (not one the user chose). Nothing
 * happens unless the live URL still names that run — a user who switched runs
 * meanwhile owns the focus, and their choice must survive.
 */
export function useReleaseStudioRun(): (runId: string) => void {
  const navigate = useNavigate();
  const location = useLocation();
  return useCallback(
    (runId: string) => {
      const search = liveSearch(location.search);
      if (new URLSearchParams(search).get("wr") !== runId) return;
      useRunViewStore.getState().reset();
      // Replace, not push: a release is not a user navigation, and pushing it
      // would let Back re-focus the run we just let go of.
      navigate({ search: searchWithRunCleared(search) }, { replace: true });
    },
    [navigate, location.search],
  );
}

/**
 * Switch the studio's inspected run from a user action (e.g. the Past Runs
 * list). The single place run-switch navigation lives, so surfaces that touch
 * it stay consistent. A pinned frame belongs to the run being left, so it is
 * dropped before the switch; RunView re-resolves the new run's selection.
 *
 * `replace` and `systemFocus` are for a caller that focuses a run on the
 * user's behalf rather than at their request — same reason useReleaseStudioRun
 * replaces. `systemFocus` keeps the layout in whatever class it already had,
 * so following the run never remaps the user's pane arrangement.
 */
export function useSwitchStudioRun(options?: {
  replace?: boolean;
  systemFocus?: boolean;
}): (runId: string) => void {
  const navigate = useNavigate();
  const location = useLocation();
  const studioRunId = useStudioRunId();
  const replace = options?.replace ?? false;
  const systemFocus = options?.systemFocus ?? false;
  return useCallback(
    (runId: string) => {
      useRunViewStore.getState().reset();
      // Under /runs/{wr} the inspected run lives in the path and the search is
      // empty, so the raw search cannot tell whether the user already owns a
      // run; resolve against the same effective search the pane layout uses.
      const effectiveSearch = searchWithRunReference(
        liveSearch(location.search),
        studioRunId,
      );
      // Push by default (unlike the pane-toggle writes in useStudioPanes): a
      // run switch the user asked for is a real navigation, so browser
      // back/forward steps through the runs they have viewed.
      navigate(
        {
          search: searchWithRunSwitched(effectiveSearch, runId, {
            systemFocus,
          }),
        },
        { replace },
      );
    },
    [navigate, location.search, studioRunId, replace, systemFocus],
  );
}
