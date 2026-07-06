import { useCallback, useMemo } from "react";
import {
  useLocation,
  useNavigate,
  type NavigateOptions,
} from "react-router-dom";

import { useStudioShellStore } from "@/store/StudioShellStore";

import { liveSearch } from "./liveSearch";
import {
  layoutClassForSearch,
  panesListEqual,
  panesWithoutDeletedBlocked,
  resolveOpenPanes,
  searchWithPanes,
  togglePane as togglePaneIn,
  withPaneClosed,
  withPaneOpen,
  type StudioPaneId,
} from "./panes";
import { useStudioPaneDefaults } from "./StudioPaneDefaultsContext";
import { useStudioWorkflowDeletedAt } from "./StudioShellContext";

type ApplyPanesOptions = Pick<NavigateOptions, "state"> & {
  // When true the resulting pane list is stored as the learned default for this
  // layout class (edit/run). System writes leave this unset so they never
  // overwrite a user's last-chosen arrangement.
  learn?: boolean;
};

/**
 * Pane state lives in the URL (?panes=), so the open set and its order are
 * shareable and can never drift from navigation. Writes merge against the live
 * URL rather than this render's closure: pushState is synchronous, so a
 * concurrent navigate() (e.g. a block-run launch) is already visible there.
 */
export function useStudioPanes() {
  const location = useLocation();
  const navigate = useNavigate();
  const { defaultPanes, clamp, notePaneWrite } = useStudioPaneDefaults();
  const workflowDeleted = useStudioWorkflowDeletedAt() !== null;
  const setPaneLayout = useStudioShellStore((s) => s.setPaneLayout);

  // The mount-time viewport clamp only masks the exact pane list the URL
  // carried at mount; any other list means someone navigated, so present it
  // as-is. The first write clears the clamp for good. A deleted source agent
  // additionally drops the workflow-mutating panes, on reads and writes alike,
  // so deep links and openPane callers degrade to the run-viewing surfaces.
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

  const panes = useMemo(
    () => present(resolveOpenPanes(location.search, defaultPanes)),
    [location.search, defaultPanes, present],
  );

  // The open list as the live URL resolves it right now — what cross-route
  // writers (block ▶, the Run form round-trip) must build on, per the
  // continuity rule: in-app actions append, never rearrange or close.
  const resolveLivePanes = useCallback(
    (): StudioPaneId[] =>
      present(resolveOpenPanes(liveSearch(location.search), defaultPanes)),
    [location.search, defaultPanes, present],
  );

  const applyPanes = useCallback(
    (
      compute: (current: StudioPaneId[]) => StudioPaneId[],
      options?: ApplyPanesOptions,
    ) => {
      const search = liveSearch(location.search);
      const current = resolveLivePanes();
      const computed = compute(current);
      const next = workflowDeleted
        ? panesWithoutDeletedBlocked(computed)
        : computed;
      notePaneWrite({ previous: current, next });
      navigate(
        { search: searchWithPanes(search, next) },
        { replace: true, state: options?.state },
      );
      if (options?.learn && next.length > 0 && !workflowDeleted) {
        const cls = layoutClassForSearch(search);
        if (cls !== null) {
          setPaneLayout(cls, next);
        }
      }
    },
    [
      navigate,
      location.search,
      resolveLivePanes,
      notePaneWrite,
      workflowDeleted,
      setPaneLayout,
    ],
  );

  const togglePane = useCallback(
    (id: StudioPaneId, opts?: Pick<ApplyPanesOptions, "learn">) =>
      applyPanes((current) => togglePaneIn(current, id), opts),
    [applyPanes],
  );

  const openPane = useCallback(
    (id: StudioPaneId, options?: ApplyPanesOptions) =>
      applyPanes((current) => withPaneOpen(current, id), options),
    [applyPanes],
  );

  const closePane = useCallback(
    (id: StudioPaneId, opts?: Pick<ApplyPanesOptions, "learn">) =>
      applyPanes((current) => withPaneClosed(current, id), opts),
    [applyPanes],
  );

  // Layout override: open exactly this list (explicit moments like the
  // version-history editor-only view), replacing whatever is open.
  const setOpenPanes = useCallback(
    (panes: readonly StudioPaneId[]) => applyPanes(() => [...panes]),
    [applyPanes],
  );

  // Reorder-only write (drag-and-drop / keyboard move): the live URL keeps
  // deciding WHICH panes are open; `order` only decides where they sit.
  const setPanesOrder = useCallback(
    (order: readonly StudioPaneId[], opts?: Pick<ApplyPanesOptions, "learn">) =>
      applyPanes((current) => {
        const next = order.filter(
          (id, index) => current.includes(id) && order.indexOf(id) === index,
        );
        for (const id of current) {
          if (!next.includes(id)) {
            next.push(id);
          }
        }
        return next;
      }, opts),
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
