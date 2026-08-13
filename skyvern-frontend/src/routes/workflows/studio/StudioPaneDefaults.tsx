import { useCallback, useMemo, useRef, useState, type ReactNode } from "react";
import { useLocation } from "react-router-dom";

import { toast } from "@/components/ui/use-toast";
import { useStudioFirstRunStore } from "@/store/StudioFirstRunStore";
import { useStudioShellStore } from "@/store/StudioShellStore";

import { liveSearch } from "./liveSearch";
import {
  copilotContextForSearch,
  DEFAULT_STUDIO_PANES,
  fitPanesToWidth,
  panesFitWidth,
  resolveOpenPanes,
  searchWithRunReference,
  STUDIO_PANE_IDS,
  type StudioPaneId,
  withCopilotSelection,
} from "./panes";
import {
  StudioPaneDefaultsContext,
  type PaneClamp,
  type PaneWrite,
} from "./StudioPaneDefaultsContext";
import { useStudioRunId } from "./useStudioRunId";
import { useStudioWorkflowDeletedAt } from "./StudioShellContext";

// Drop unknown ids and duplicates; return null when the result is empty/invalid.
function sanitizeLearnedPanes(
  raw: StudioPaneId[] | undefined,
): readonly StudioPaneId[] | null {
  // Guard the localStorage boundary: corrupted/foreign values may not be arrays.
  if (!Array.isArray(raw) || raw.length === 0) return null;
  const seen = new Set<string>();
  const result: StudioPaneId[] = [];
  for (const id of raw) {
    if ((STUDIO_PANE_IDS as readonly string[]).includes(id) && !seen.has(id)) {
      seen.add(id);
      result.push(id);
    }
  }
  return result.length > 0 ? result : null;
}

/**
 * First-visit pane policy for one studio mount. The state-aware default and
 * the narrow-viewport clamp are both latched exactly once (the shell remounts
 * per workflow via its key), so panes never reshuffle after first paint. The
 * blocks signal is synchronous — the shell only mounts with the workflow
 * loaded — so the latch decides from real data, never a placeholder.
 *
 * Built agents restore the user's last edit-class pane arrangement; empty
 * agents always start on the factory Editor + Browser default.
 */
export function StudioPaneDefaultsProvider({
  hasBlocks,
  children,
}: {
  hasBlocks: boolean;
  children: ReactNode;
}) {
  const location = useLocation();
  const studioRunId = useStudioRunId();
  const workflowDeleted = useStudioWorkflowDeletedAt() !== null;

  // hasBlocks gates only the learned-layout lookup, not the factory default
  // itself — empty agents never restore a saved arrangement.
  const [defaultPanes] = useState<readonly StudioPaneId[]>(() => {
    if (!hasBlocks) {
      return [...DEFAULT_STUDIO_PANES];
    }
    const learnedEdit = sanitizeLearnedPanes(
      useStudioShellStore.getState().paneLayouts["edit"],
    );
    return learnedEdit ?? [...DEFAULT_STUDIO_PANES];
  });

  const [learnedRunPanes] = useState<readonly StudioPaneId[] | null>(() =>
    sanitizeLearnedPanes(useStudioShellStore.getState().paneLayouts.run),
  );

  const [initialUrlPanes] = useState<readonly StudioPaneId[]>(() =>
    resolveOpenPanes(
      searchWithRunReference(liveSearch(location.search), studioRunId),
      defaultPanes,
      learnedRunPanes,
    ),
  );

  const [initialPanes] = useState<readonly StudioPaneId[]>(() => {
    const search = searchWithRunReference(
      liveSearch(location.search),
      studioRunId,
    );
    const resolved = initialUrlPanes;
    const selection =
      useStudioShellStore.getState().copilotSelectionByLayout[
        copilotContextForSearch(search)
      ];
    return workflowDeleted
      ? resolved
      : withCopilotSelection(resolved, selection);
  });

  const [clamp, setClamp] = useState<PaneClamp | null>(null);
  const stageElRef = useRef<HTMLElement | null>(null);
  const measuredRef = useRef(false);
  const wroteRef = useRef(false);

  // Ref callbacks run before paint, so an over-wide shared link is clamped to
  // its fitting prefix without ever flashing the full list.
  const registerStageElement = useCallback(
    (el: HTMLElement | null) => {
      stageElRef.current = el;
      if (!el || measuredRef.current || wroteRef.current) {
        return;
      }
      const width = el.clientWidth;
      if (width <= 0) {
        return;
      }
      measuredRef.current = true;
      const fitted = fitPanesToWidth(initialPanes, width);
      const urlFitted = fitPanesToWidth(initialUrlPanes, width);
      if (
        fitted.length !== initialPanes.length ||
        urlFitted.length !== initialUrlPanes.length
      ) {
        setClamp({
          source: initialPanes,
          presented: fitted,
          urlSource: initialUrlPanes,
          urlPresented: urlFitted,
        });
      }
    },
    [initialPanes, initialUrlPanes],
  );

  const notePaneWrite = useCallback((change: PaneWrite) => {
    wroteRef.current = true;
    setClamp((current) => {
      if (current === null) {
        return current;
      }
      if (change.nextRuntimeSource !== undefined) {
        return {
          ...current,
          source: [...change.nextRuntimeSource],
          presented: [...change.next],
        };
      }
      return null;
    });
    const firstRun = useStudioFirstRunStore.getState();
    // Toggling a pane is the lesson the coach mark teaches.
    if (!firstRun.coachMarkSeen) {
      firstRun.markCoachMarkSeen();
    }
    const stageWidth = stageElRef.current?.clientWidth ?? 0;
    if (
      stageWidth > 0 &&
      change.next.length > change.previous.length &&
      !panesFitWidth(change.next, stageWidth) &&
      !firstRun.narrowNudgeSeen
    ) {
      firstRun.markNarrowNudgeSeen();
      toast({
        title: "Tight fit",
        description:
          "Panes keep a minimum width, so this view may feel cramped. Close a pane from the left rail any time.",
      });
    }
  }, []);

  const value = useMemo(
    () => ({
      defaultPanes,
      clamp,
      notePaneWrite,
      registerStageElement,
      learnedRunPanes,
    }),
    [defaultPanes, clamp, notePaneWrite, registerStageElement, learnedRunPanes],
  );

  return (
    <StudioPaneDefaultsContext.Provider value={value}>
      {children}
    </StudioPaneDefaultsContext.Provider>
  );
}
