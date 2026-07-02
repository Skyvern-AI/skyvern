import { useCallback, useMemo, useRef, useState, type ReactNode } from "react";
import { useLocation } from "react-router-dom";

import { toast } from "@/components/ui/use-toast";
import { useStudioFirstRunStore } from "@/store/StudioFirstRunStore";

import { liveSearch } from "./liveSearch";
import {
  defaultPanesForWorkflowState,
  fitPanesToWidth,
  panesFitWidth,
  resolveOpenPanes,
  type StudioPaneId,
} from "./panes";
import {
  StudioPaneDefaultsContext,
  type PaneClamp,
  type PaneWrite,
} from "./StudioPaneDefaultsContext";
import { useStudioRunSignals } from "./useStudioRunSignals";

/**
 * First-visit pane policy for one studio mount. The state-aware default and
 * the narrow-viewport clamp are both latched exactly once (the shell remounts
 * per workflow via its key), so panes never reshuffle after first paint: a
 * runs signal that arrives later changes nothing until the next visit.
 */
export function StudioPaneDefaultsProvider({
  hasBlocks,
  children,
}: {
  hasBlocks: boolean;
  children: ReactNode;
}) {
  const location = useLocation();
  const { knownHasRuns } = useStudioRunSignals();

  // Latched: cached runs data decides; on a cold cache an agent with blocks
  // keeps today's watch default while an empty agent starts on the editor.
  const [defaultPanes] = useState<readonly StudioPaneId[]>(() =>
    defaultPanesForWorkflowState({ hasRuns: knownHasRuns, hasBlocks }),
  );

  const [initialPanes] = useState<readonly StudioPaneId[]>(() =>
    resolveOpenPanes(liveSearch(location.search), defaultPanes),
  );

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
      if (fitted.length !== initialPanes.length) {
        setClamp({ source: initialPanes, presented: fitted });
      }
    },
    [initialPanes],
  );

  const notePaneWrite = useCallback((change: PaneWrite) => {
    wroteRef.current = true;
    setClamp((current) => (current === null ? current : null));
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
    () => ({ defaultPanes, clamp, notePaneWrite, registerStageElement }),
    [defaultPanes, clamp, notePaneWrite, registerStageElement],
  );

  return (
    <StudioPaneDefaultsContext.Provider value={value}>
      {children}
    </StudioPaneDefaultsContext.Provider>
  );
}
