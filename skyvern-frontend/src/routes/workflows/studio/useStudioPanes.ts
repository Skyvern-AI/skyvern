import { useCallback, useMemo } from "react";
import {
  useLocation,
  useNavigate,
  type NavigateOptions,
} from "react-router-dom";

import { useStudioShellStore } from "@/store/StudioShellStore";
import { liveLocationState, liveSearch } from "./liveSearch";
import {
  copilotContextForSearch,
  layoutClassForSearch,
  panesListEqual,
  panesWithoutDeletedBlocked,
  resolveOpenPanes,
  searchWithPanes,
  searchWithRunReference,
  toReadableSearch,
  togglePane as togglePaneIn,
  withCopilotSelection,
  withPaneClosed,
  withPaneOpen,
  type CopilotPaneSelection,
  type StudioPaneId,
} from "./panes";
import { SELECTED_BLOCK_SEARCH_PARAM } from "../editor/hooks/useSelectedBlockUrlSync";
import { useStudioPaneDefaults } from "./StudioPaneDefaultsContext";
import { useStudioRunId } from "./useStudioRunId";
import { useStudioWorkflowDeletedAt } from "./StudioShellContext";

type ApplyPanesOptions = Pick<NavigateOptions, "state"> & {
  // When true the resulting pane list is stored as the learned default for this
  // layout class (edit/run). System writes leave this unset so they never
  // overwrite a user's last-chosen arrangement.
  learn?: boolean;
  // Written in the SAME navigation as the pane change. null clears the value;
  // undefined leaves it untouched.
  selectedBlockLabel?: string | null;
};

function withSelectedBlockLabel(
  search: string,
  selectedBlockLabel: string | null | undefined,
): string {
  if (selectedBlockLabel === undefined) {
    return search;
  }
  const params = new URLSearchParams(search);
  if (selectedBlockLabel === null) {
    params.delete(SELECTED_BLOCK_SEARCH_PARAM);
  } else {
    params.set(SELECTED_BLOCK_SEARCH_PARAM, selectedBlockLabel);
  }
  return toReadableSearch(params);
}

type PaneWriteKind = "normal" | "copilot-only" | "exact" | "reorder";

function withUrlCopilotPreserved(
  nonCopilotPanes: readonly StudioPaneId[],
  urlPanes: readonly StudioPaneId[],
): StudioPaneId[] {
  const next = [...nonCopilotPanes];
  const copilotIndex = urlPanes.indexOf("copilot");
  if (copilotIndex !== -1) {
    next.splice(Math.min(copilotIndex, next.length), 0, "copilot");
  }
  return next;
}

/**
 * Non-Copilot pane state remains shareable in `?panes=`. Copilot selection is
 * runtime-only, with independent edit and run memories layered over that URL.
 * Cross-route writers continue to receive only the committed URL/default list.
 */
export function useStudioPanes() {
  const location = useLocation();
  const navigate = useNavigate();
  const studioRunId = useStudioRunId();
  const effectiveSearch = searchWithRunReference(location.search, studioRunId);
  const copilotContext = copilotContextForSearch(effectiveSearch);
  const copilotSelection = useStudioShellStore(
    (state) => state.copilotSelectionByLayout[copilotContext],
  );
  const setCopilotSelection = useStudioShellStore(
    (state) => state.setCopilotSelection,
  );
  const setPaneLayout = useStudioShellStore((state) => state.setPaneLayout);
  const { defaultPanes, clamp, notePaneWrite, learnedRunPanes } =
    useStudioPaneDefaults();
  const workflowDeleted = useStudioWorkflowDeletedAt() !== null;

  const present = useCallback(
    (resolved: StudioPaneId[]): StudioPaneId[] => {
      const presented =
        clamp && panesListEqual(resolved, clamp.source)
          ? [...clamp.presented]
          : resolved;
      return workflowDeleted
        ? panesWithoutDeletedBlocked(presented)
        : presented;
    },
    [clamp, workflowDeleted],
  );

  const presentUrlPanes = useCallback(
    (resolved: StudioPaneId[]): StudioPaneId[] => {
      const presented =
        clamp && panesListEqual(resolved, clamp.urlSource)
          ? [...clamp.urlPresented]
          : resolved;
      return workflowDeleted
        ? panesWithoutDeletedBlocked(presented)
        : presented;
    },
    [clamp, workflowDeleted],
  );

  const panes = useMemo(() => {
    const committed = resolveOpenPanes(
      effectiveSearch,
      defaultPanes,
      learnedRunPanes,
    );
    const selected = workflowDeleted
      ? committed
      : withCopilotSelection(committed, copilotSelection);
    return present(selected);
  }, [
    copilotSelection,
    defaultPanes,
    effectiveSearch,
    learnedRunPanes,
    present,
    workflowDeleted,
  ]);

  // Keep the established URL/default-only contract for block-run and run-form
  // transitions. A runtime Copilot choice must not leak into a destination URL.
  const resolveLivePanes = useCallback((): StudioPaneId[] => {
    const search = searchWithRunReference(
      liveSearch(location.search),
      studioRunId,
    );
    return presentUrlPanes(
      resolveOpenPanes(search, defaultPanes, learnedRunPanes),
    );
  }, [
    defaultPanes,
    learnedRunPanes,
    location.search,
    presentUrlPanes,
    studioRunId,
  ]);

  const applyPanes = useCallback(
    (
      compute: (current: StudioPaneId[]) => StudioPaneId[],
      options?: ApplyPanesOptions,
      writeKind: PaneWriteKind = "normal",
    ) => {
      const search = liveSearch(location.search);
      const resolvedSearch = searchWithRunReference(search, studioRunId);
      const context = copilotContextForSearch(resolvedSearch);
      const urlPanes = resolveOpenPanes(
        resolvedSearch,
        defaultPanes,
        learnedRunPanes,
      );
      const storedSelection =
        useStudioShellStore.getState().copilotSelectionByLayout[context];
      const selected = workflowDeleted
        ? urlPanes
        : withCopilotSelection(urlPanes, storedSelection);
      const current = present(selected);
      const currentCopilotIndex = current.indexOf("copilot");
      const rememberedCopilotIndex =
        storedSelection?.index ??
        (currentCopilotIndex === -1 ? undefined : currentCopilotIndex);
      const computed = compute(current);
      let next = workflowDeleted
        ? panesWithoutDeletedBlocked(computed)
        : computed;

      // Opening Copilot restores its remembered position instead of appending.
      if (
        writeKind === "copilot-only" &&
        currentCopilotIndex === -1 &&
        next.includes("copilot") &&
        rememberedCopilotIndex !== undefined
      ) {
        next = withCopilotSelection(next, {
          open: true,
          index: rememberedCopilotIndex,
        });
      }

      const nextCopilotIndex = next.indexOf("copilot");
      let nextSelection = storedSelection;
      if (!workflowDeleted) {
        let selection: CopilotPaneSelection | undefined;
        if (writeKind === "copilot-only" || writeKind === "exact") {
          selection = {
            open: nextCopilotIndex !== -1,
            index:
              nextCopilotIndex === -1
                ? rememberedCopilotIndex
                : nextCopilotIndex,
          };
        } else if (writeKind === "reorder" && nextCopilotIndex !== -1) {
          selection = { open: true, index: nextCopilotIndex };
        } else if (
          writeKind === "normal" &&
          storedSelection !== undefined &&
          nextCopilotIndex !== -1
        ) {
          selection = { open: true, index: nextCopilotIndex };
        }
        if (selection !== undefined) {
          nextSelection = selection;
        }
        if (
          selection !== undefined &&
          (selection.open !== storedSelection?.open ||
            selection.index !== storedSelection?.index)
        ) {
          setCopilotSelection(context, selection);
        }
      }

      const currentWithoutCopilot = current.filter(
        (pane) => pane !== "copilot",
      );
      const nextWithoutCopilot = next.filter((pane) => pane !== "copilot");
      const urlPanesForWrite = workflowDeleted
        ? panesWithoutDeletedBlocked(urlPanes)
        : urlPanes;
      // An exact override replaces the full committed non-Copilot set, even
      // when the viewport clamp currently hides part of that set. Other
      // operations build on the panes the user can see.
      const nonCopilotBaseline =
        writeKind === "exact"
          ? urlPanesForWrite.filter((pane) => pane !== "copilot")
          : currentWithoutCopilot;
      const nonCopilotChanged = !panesListEqual(
        nonCopilotBaseline,
        nextWithoutCopilot,
      );
      if (!panesListEqual(current, next)) {
        notePaneWrite({
          previous: current,
          next,
          nextRuntimeSource:
            !nonCopilotChanged && !workflowDeleted
              ? withCopilotSelection(urlPanes, nextSelection)
              : undefined,
        });
      }
      if (!nonCopilotChanged && writeKind !== "normal") {
        if (
          options !== undefined &&
          ("state" in options || options.selectedBlockLabel !== undefined)
        ) {
          navigate(
            {
              pathname: location.pathname,
              search: withSelectedBlockLabel(
                search,
                options.selectedBlockLabel,
              ),
              hash: location.hash,
            },
            { replace: true, state: options.state },
          );
        }
        return;
      }

      // Until the user makes a Copilot choice, its URL position is still the
      // source of truth. Preserve the existing write behavior for normal
      // non-Copilot actions. Once runtime memory exists, keep it out of the URL.
      const urlNext =
        writeKind === "normal" && storedSelection === undefined
          ? next
          : withUrlCopilotPreserved(nextWithoutCopilot, urlPanesForWrite);
      navigate(
        {
          pathname: location.pathname,
          search: withSelectedBlockLabel(
            searchWithPanes(search, urlNext),
            options?.selectedBlockLabel,
          ),
          hash: location.hash,
        },
        {
          replace: true,
          state:
            options !== undefined && "state" in options
              ? options.state
              : liveLocationState(location.search, location.state),
        },
      );
      if (options?.learn && urlNext.length > 0 && !workflowDeleted) {
        const cls = layoutClassForSearch(resolvedSearch);
        if (cls !== null) {
          setPaneLayout(cls, urlNext);
        }
      }
    },
    [
      defaultPanes,
      learnedRunPanes,
      location.hash,
      location.pathname,
      location.search,
      location.state,
      navigate,
      notePaneWrite,
      present,
      setCopilotSelection,
      setPaneLayout,
      workflowDeleted,
      studioRunId,
    ],
  );

  const togglePane = useCallback(
    (id: StudioPaneId, opts?: Pick<ApplyPanesOptions, "learn">) =>
      applyPanes(
        (current) => togglePaneIn(current, id),
        opts,
        id === "copilot" ? "copilot-only" : "normal",
      ),
    [applyPanes],
  );

  const openPane = useCallback(
    (id: StudioPaneId, options?: ApplyPanesOptions) =>
      applyPanes(
        (current) => withPaneOpen(current, id),
        options,
        id === "copilot" ? "copilot-only" : "normal",
      ),
    [applyPanes],
  );

  const closePane = useCallback(
    (id: StudioPaneId, opts?: Pick<ApplyPanesOptions, "learn">) =>
      applyPanes(
        (current) => withPaneClosed(current, id),
        opts,
        id === "copilot" ? "copilot-only" : "normal",
      ),
    [applyPanes],
  );

  const setOpenPanes = useCallback(
    (panes: readonly StudioPaneId[]) =>
      applyPanes(() => [...panes], undefined, "exact"),
    [applyPanes],
  );

  const setPanesOrder = useCallback(
    (order: readonly StudioPaneId[], opts?: Pick<ApplyPanesOptions, "learn">) =>
      applyPanes(
        (current) => {
          const next = order.filter(
            (id, index) => current.includes(id) && order.indexOf(id) === index,
          );
          for (const id of current) {
            if (!next.includes(id)) {
              next.push(id);
            }
          }
          return next;
        },
        opts,
        "reorder",
      ),
    [applyPanes],
  );

  return {
    panes,
    resolveLivePanes,
    togglePane,
    openPane,
    closePane,
    setOpenPanes,
    setPanesOrder,
  };
}
