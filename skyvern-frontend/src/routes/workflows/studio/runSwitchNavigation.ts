import { useCallback } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { useRunViewStore } from "@/store/RunViewStore";

import { liveSearch } from "./liveSearch";
import {
  fitPanesToWidth,
  panesFitWidth,
  searchWithRunReference,
  SYSTEM_RUN_FOCUS_PARAM,
  toReadableSearch,
  withPaneOpen,
  type StudioPaneId,
} from "./panes";
import { useStudioRunId } from "./useStudioRunId";
import { useStudioPaneDefaults } from "./StudioPaneDefaultsContext";

// Which panes a Copilot run keeps on a narrow stage, first to last.
const NARROW_RUN_PANE_RANK: Record<StudioPaneId, number> = {
  browser: 0,
  copilot: 1,
  overview: 2,
  editor: 3,
};

// Point the studio at a different run: set ?wr=, drop the per-run selection
// params (?active=, ?bl=, ?iteration=). User navigation drops pane overrides.
// The caller merges this
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
  if (!options?.systemFocus) params.delete("panes");
  if (options?.systemFocus && copilotOwnsFocus) {
    params.set(SYSTEM_RUN_FOCUS_PARAM, "copilot");
  } else {
    // A user switching runs takes over the focus; the layout reclassifies.
    params.delete(SYSTEM_RUN_FOCUS_PARAM);
  }
  params.delete("active");
  params.delete("bl");
  // A loop-iteration scope belongs to the run being left; inert in studio
  // today, but clearing it keeps this the single, complete home for run-switch
  // navigation.
  params.delete("iteration");
  return toReadableSearch(params);
}

// Point the studio back at its live browser: drop the inspected run and its per-run selection.
export function searchWithoutRun(search: string): string {
  const params = new URLSearchParams(search);
  for (const key of [
    "wr",
    SYSTEM_RUN_FOCUS_PARAM,
    "active",
    "bl",
    "iteration",
  ]) {
    params.delete(key);
  }
  return toReadableSearch(params);
}

/**
 * Switch the studio's inspected run from a user action (e.g. the Past Runs
 * list). The single place run-switch navigation lives, so surfaces that touch
 * it stay consistent. A pinned frame belongs to the run being left, so it is
 * dropped before the switch; RunView re-resolves the new run's selection.
 *
 * `replace` and `systemFocus` are for a caller that focuses a run on the
 * user's behalf rather than at their request, so Back never re-focuses it.
 * `systemFocus` keeps the layout in whatever class it already had,
 * so following the run never remaps the user's pane arrangement; it only adds
 * the Browser pane, so a Copilot test run started from Edit is visible.
 */
export function useSwitchStudioRun(options?: {
  replace?: boolean;
  systemFocus?: boolean;
}): (runId: string) => void {
  const navigate = useNavigate();
  const location = useLocation();
  const { preserveNextEntry, updatePanes } = useStudioPaneDefaults();
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
      const nextSearch = searchWithRunSwitched(effectiveSearch, runId, {
        systemFocus,
      });
      if (systemFocus) {
        updatePanes((panes, slots, stageWidth) => {
          if (panes.includes("browser")) return panes;
          const opened = withPaneOpen(panes, "browser", slots);
          if (stageWidth <= 0 || panesFitWidth(opened, stageWidth)) {
            return opened;
          }
          // Narrow stage: keep Browser, then Copilot, then whatever else fits,
          // with Editor dropped first (the sign-in rule); keep on-screen order.
          const priority = [...opened].sort(
            (a, b) => NARROW_RUN_PANE_RANK[a] - NARROW_RUN_PANE_RANK[b],
          );
          const kept = fitPanesToWidth(priority, stageWidth);
          return {
            panes: opened.filter((id) => kept.includes(id)),
            arrangement: opened,
          };
        });
      }
      preserveNextEntry(systemFocus ? nextSearch : null);
      navigate({ search: nextSearch }, { replace });
    },
    [
      navigate,
      location.search,
      studioRunId,
      replace,
      systemFocus,
      preserveNextEntry,
      updatePanes,
    ],
  );
}
