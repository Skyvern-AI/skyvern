import {
  useCallback,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useLocation } from "react-router-dom";

import { toast } from "@/components/ui/use-toast";
import { useFirstParam } from "@/hooks/useFirstParam";
import { useStudioFirstRunStore } from "@/store/StudioFirstRunStore";
import { sanitizePaneWidth, type PaneWidths } from "@/store/paneWidths";

import {
  CREATE_STUDIO_PANES,
  DEFAULT_STUDIO_PANES,
  fitPanesToWidth,
  panesFitWidth,
  panesListEqual,
  panesWithoutDeletedBlocked,
  resolveOpenPanes,
  searchWithRunReference,
  type StudioPaneId,
} from "./panes";
import { StudioPaneDefaultsContext } from "./StudioPaneDefaultsContext";
import { useStudioWorkflowDeletedAt } from "./StudioShellContext";

type PaneState = {
  key: string;
  panes: StudioPaneId[];
  paneWidths: PaneWidths;
  entryId: number;
};

export function StudioPaneDefaultsProvider({
  hasBlocks,
  children,
}: {
  hasBlocks: boolean;
  children: ReactNode;
}) {
  const location = useLocation();
  const pathRunId = useFirstParam("workflowRunId", "runId");
  const workflowDeleted = useStudioWorkflowDeletedAt() !== null;
  const stageElRef = useRef<HTMLElement | null>(null);
  const transitionRef = useRef<{
    key: string;
    panes?: readonly StudioPaneId[];
  } | null>(null);
  const locationKeyRef = useRef(location.key);
  const measuredEntryRef = useRef<number | null>(null);
  const wroteEntryRef = useRef<number | null>(null);

  const keyForSearch = useCallback(
    (search: string) => {
      const params = new URLSearchParams(
        searchWithRunReference(search, pathRunId),
      );
      return JSON.stringify([
        location.pathname,
        params.get("wr") ?? (params.has("active") ? "active" : null),
        params.get("panes"),
        params.get("embed") === "true" ? ["embed", params.get("view")] : null,
      ]);
    },
    [location.pathname, pathRunId],
  );
  const key = keyForSearch(location.search);

  const initialPanes = () => {
    const resolved = resolveOpenPanes(
      searchWithRunReference(location.search, pathRunId),
      hasBlocks ? DEFAULT_STUDIO_PANES : CREATE_STUDIO_PANES,
    );
    const panes = workflowDeleted
      ? panesWithoutDeletedBlocked(resolved)
      : resolved;
    const width = stageElRef.current?.clientWidth ?? 0;
    return width > 0 ? fitPanesToWidth(panes, width) : panes;
  };
  const [state, setState] = useState<PaneState>(() => ({
    key,
    panes: initialPanes(),
    paneWidths: {},
    entryId: 0,
  }));
  const latestRef = useRef(state);
  let current = state;
  if (state.key !== key) {
    const transition = transitionRef.current;
    current =
      transition?.key === key
        ? {
            ...state,
            key,
            panes: transition.panes ? [...transition.panes] : state.panes,
          }
        : {
            key,
            panes: initialPanes(),
            paneWidths: {},
            entryId: state.entryId + 1,
          };
    setState(current);
  }
  latestRef.current = current;
  // React can restart a navigation render before committing its state.
  useLayoutEffect(() => {
    if (locationKeyRef.current !== location.key) {
      transitionRef.current = null;
      locationKeyRef.current = location.key;
    }
  }, [location.key]);

  const getPanes = useCallback(() => latestRef.current.panes, []);
  const updatePanes = useCallback(
    (compute: (panes: StudioPaneId[]) => StudioPaneId[]) => {
      const previous = latestRef.current;
      const computed = compute([...previous.panes]);
      const panes = workflowDeleted
        ? panesWithoutDeletedBlocked(computed)
        : computed;
      wroteEntryRef.current = previous.entryId;
      if (!panesListEqual(previous.panes, panes)) {
        latestRef.current = { ...previous, panes };
        setState(latestRef.current);
      }
      const firstRun = useStudioFirstRunStore.getState();
      if (!firstRun.coachMarkSeen) firstRun.markCoachMarkSeen();
      const width = stageElRef.current?.clientWidth ?? 0;
      if (
        width > 0 &&
        panes.length > previous.panes.length &&
        !panesFitWidth(panes, width) &&
        !firstRun.narrowNudgeSeen
      ) {
        firstRun.markNarrowNudgeSeen();
        toast({
          title: "Tight fit",
          description:
            "Panes keep a minimum width, so this view may feel cramped. Close a pane from the left rail any time.",
        });
      }
    },
    [workflowDeleted],
  );

  const registerStageElement = useCallback((el: HTMLElement | null) => {
    stageElRef.current = el;
    const current = latestRef.current;
    if (
      !el ||
      el.clientWidth <= 0 ||
      measuredEntryRef.current === current.entryId ||
      wroteEntryRef.current === current.entryId
    )
      return;
    measuredEntryRef.current = current.entryId;
    const panes = fitPanesToWidth(current.panes, el.clientWidth);
    if (!panesListEqual(current.panes, panes)) {
      latestRef.current = { ...current, panes };
      setState(latestRef.current);
    }
  }, []);

  const setPaneWidths = useCallback((widths: PaneWidths) => {
    const paneWidths = { ...latestRef.current.paneWidths };
    for (const [id, raw] of Object.entries(widths)) {
      const width = sanitizePaneWidth(raw);
      if (width !== undefined) paneWidths[id] = width;
    }
    latestRef.current = { ...latestRef.current, paneWidths };
    setState(latestRef.current);
  }, []);
  const resetPaneWidths = useCallback(() => {
    latestRef.current = { ...latestRef.current, paneWidths: {} };
    setState(latestRef.current);
  }, []);
  const preserveNextEntry = useCallback(
    (search: string | null, panes?: readonly StudioPaneId[]) => {
      if (search === null) {
        transitionRef.current = null;
        return;
      }
      const key = keyForSearch(search);
      if (key === latestRef.current.key) {
        transitionRef.current = null;
        if (panes) updatePanes(() => [...panes]);
      } else {
        transitionRef.current = { key, panes };
      }
    },
    [keyForSearch, updatePanes],
  );

  const value = useMemo(
    () => ({
      isStudio: true,
      panes: current.panes,
      paneWidths: current.paneWidths,
      entryId: current.entryId,
      getPanes,
      updatePanes,
      registerStageElement,
      setPaneWidths,
      resetPaneWidths,
      preserveNextEntry,
    }),
    [
      current.panes,
      current.paneWidths,
      current.entryId,
      getPanes,
      updatePanes,
      registerStageElement,
      setPaneWidths,
      resetPaneWidths,
      preserveNextEntry,
    ],
  );
  return (
    <StudioPaneDefaultsContext.Provider value={value}>
      {children}
    </StudioPaneDefaultsContext.Provider>
  );
}
