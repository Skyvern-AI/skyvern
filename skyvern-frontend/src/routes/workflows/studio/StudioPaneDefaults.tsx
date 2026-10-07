import {
  useCallback,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { toast } from "@/components/ui/use-toast";
import { useFirstParam } from "@/hooks/useFirstParam";
import { useMountEffect } from "@/hooks/useMountEffect";
import { useStudioFirstRunStore } from "@/store/StudioFirstRunStore";
import { useManualSignInStore } from "@/store/useManualSignInStore";
import { sanitizePaneWidth, type PaneWidths } from "@/store/paneWidths";

import {
  CREATE_STUDIO_PANES,
  DEFAULT_STUDIO_PANES,
  fitPanesToWidth,
  panesFitWidth,
  panesListEqual,
  panesWithoutDeletedBlocked,
  rememberPaneSlots,
  resolveOpenPanes,
  searchWithRunReference,
  toReadableSearch,
  withPaneOpen,
  type StudioPaneId,
} from "./panes";
import { liveLocationState, liveSearch } from "./liveSearch";
import {
  StudioPaneDefaultsContext,
  type PanesCutFrom,
} from "./StudioPaneDefaultsContext";
import { useStudioWorkflowDeletedAt } from "./StudioShellContext";

// Create markers nothing reads after mount; discover, template and record are
// stripped later by the flows that consume them.
const CREATE_ONLY_VIA = ["blank", "sidebar", "onboarding_template"];

type PaneState = {
  key: string;
  panes: StudioPaneId[];
  slots: StudioPaneId[];
  paneWidths: PaneWidths;
  entryId: number;
};

function withPanes(
  state: PaneState,
  panes: StudioPaneId[],
  arrangement?: readonly StudioPaneId[],
): PaneState {
  const slots = arrangement
    ? rememberPaneSlots(state.slots, arrangement)
    : state.slots;
  return { ...state, panes, slots: rememberPaneSlots(slots, panes) };
}

export function StudioPaneDefaultsProvider({
  hasBlocks,
  children,
}: {
  hasBlocks: boolean;
  children: ReactNode;
}) {
  const location = useLocation();
  const navigate = useNavigate();
  const pathRunId = useFirstParam("workflowRunId", "runId");
  // A Copilot manual sign-in happens in the Browser pane, which Edit does not
  // open; a re-resolve during the sign-in (it can drop ?wr=) keeps it too. The
  // store is global, so only a sign-in that starts after this studio mounts counts.
  const signingInRef = useRef(false);
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
    // Create flows mark their entry with ?via= or ?record=, and some strip it only
    // after this first resolve. An agent opened without one is being edited.
    const params = new URLSearchParams(location.search);
    const creating = params.has("via") || params.get("record") === "1";
    const defaults = !creating
      ? DEFAULT_STUDIO_PANES
      : hasBlocks
        ? withPaneOpen(CREATE_STUDIO_PANES, "editor")
        : CREATE_STUDIO_PANES;
    const resolved = resolveOpenPanes(
      searchWithRunReference(location.search, pathRunId),
      defaults,
    );
    // Before Editor, so the narrow-stage clamp (it keeps a leading prefix)
    // drops Editor rather than the Browser the user is signing in through.
    const shown = signingInRef.current
      ? withPaneOpen(resolved, "browser", ["browser", "editor"])
      : resolved;
    const panes = workflowDeleted ? panesWithoutDeletedBlocked(shown) : shown;
    const width = stageElRef.current?.clientWidth ?? 0;
    return width > 0 ? fitPanesToWidth(panes, width) : panes;
  };
  const [state, setState] = useState<PaneState>(() => {
    const panes = initialPanes();
    return { key, panes, slots: [...panes], paneWidths: {}, entryId: 0 };
  });
  // Same-commit readers (pane resolve, studio telemetry) already captured it;
  // dropping it lets a reload or shared link open the Edit panes.
  useMountEffect(() => {
    const params = new URLSearchParams(liveSearch(location.search));
    if (!CREATE_ONLY_VIA.includes(params.get("via") ?? "")) return;
    params.delete("via");
    navigate(
      {
        pathname: location.pathname,
        search: toReadableSearch(params),
        hash: location.hash,
      },
      {
        replace: true,
        state: liveLocationState(location.search, location.state),
      },
    );
  });
  const latestRef = useRef(state);
  let current = state;
  if (state.key !== key) {
    const transition = transitionRef.current;
    if (transition?.key === key) {
      current = withPanes(
        { ...state, key },
        transition.panes ? [...transition.panes] : state.panes,
      );
    } else {
      const panes = initialPanes();
      current = {
        key,
        panes,
        slots: [...panes],
        paneWidths: {},
        entryId: state.entryId + 1,
      };
    }
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
    (
      compute: (
        panes: StudioPaneId[],
        slots: readonly StudioPaneId[],
        stageWidth: number,
      ) => StudioPaneId[] | PanesCutFrom,
    ) => {
      const previous = latestRef.current;
      const result = compute(
        [...previous.panes],
        previous.slots,
        stageElRef.current?.clientWidth ?? 0,
      );
      const computed = Array.isArray(result) ? result : result.panes;
      const arrangement = Array.isArray(result)
        ? undefined
        : result.arrangement;
      const panes = workflowDeleted
        ? panesWithoutDeletedBlocked(computed)
        : computed;
      wroteEntryRef.current = previous.entryId;
      if (!panesListEqual(previous.panes, panes)) {
        latestRef.current = withPanes(previous, panes, arrangement);
        setState(latestRef.current);
      }
      const firstRun = useStudioFirstRunStore.getState();
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
  // A layout effect subscribes before the Copilot chat's passive effects write
  // the store, and before a previous agent's chat clears it on unmount.
  useLayoutEffect(
    () =>
      useManualSignInStore.subscribe((state, previous) => {
        if (state.browserSessionId === previous.browserSessionId) return;
        signingInRef.current = state.browserSessionId !== null;
        if (signingInRef.current) {
          updatePanes((panes, slots) => withPaneOpen(panes, "browser", slots));
        }
      }),
    [updatePanes],
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
      latestRef.current = withPanes(current, panes);
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
