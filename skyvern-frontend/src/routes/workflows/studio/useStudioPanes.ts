import { useCallback, useMemo } from "react";
import {
  useLocation,
  useNavigate,
  type NavigateOptions,
} from "react-router-dom";

import { liveLocationState, liveSearch } from "./liveSearch";
import {
  panesWithoutDeletedBlocked,
  resolveOpenPanes,
  searchWithRunReference,
  toReadableSearch,
  togglePane as togglePaneIn,
  withPaneClosed,
  withPaneOpen,
  type StudioPaneId,
} from "./panes";
import { SELECTED_BLOCK_SEARCH_PARAM } from "../editor/hooks/useSelectedBlockUrlSync";
import { useStudioPaneDefaults } from "./StudioPaneDefaultsContext";
import { useStudioRunId } from "./useStudioRunId";
import {
  useStudioShellContext,
  useStudioWorkflowDeletedAt,
} from "./StudioShellContext";

type ApplyPanesOptions = Pick<NavigateOptions, "state"> & {
  selectedBlockLabel?: string | null;
};

export function useStudioPanes() {
  const location = useLocation();
  const navigate = useNavigate();
  const studioRunId = useStudioRunId();
  const {
    isStudio,
    panes: currentPanes,
    getPanes,
    updatePanes,
    preserveNextEntry,
  } = useStudioPaneDefaults();
  const workflowDeleted = useStudioWorkflowDeletedAt() !== null;
  const { restoreExpandedPane } = useStudioShellContext();
  const panes = useMemo(() => {
    const resolved = isStudio
      ? [...currentPanes]
      : resolveOpenPanes(searchWithRunReference(location.search, studioRunId));
    return workflowDeleted ? panesWithoutDeletedBlocked(resolved) : resolved;
  }, [isStudio, currentPanes, location.search, studioRunId, workflowDeleted]);
  const resolveLivePanes = useCallback((): StudioPaneId[] => {
    const resolved = isStudio
      ? [...getPanes()]
      : resolveOpenPanes(
          searchWithRunReference(liveSearch(location.search), studioRunId),
        );
    return workflowDeleted ? panesWithoutDeletedBlocked(resolved) : resolved;
  }, [isStudio, getPanes, location.search, studioRunId, workflowDeleted]);

  const applyPanes = useCallback(
    (
      compute: (current: StudioPaneId[]) => StudioPaneId[],
      options?: ApplyPanesOptions,
    ) => {
      restoreExpandedPane?.();
      updatePanes(compute);
      if (
        !options ||
        (!("state" in options) && options.selectedBlockLabel === undefined)
      )
        return;
      const search = liveSearch(location.search);
      const params = new URLSearchParams(search);
      if (options.selectedBlockLabel === null)
        params.delete(SELECTED_BLOCK_SEARCH_PARAM);
      else if (options.selectedBlockLabel !== undefined)
        params.set(SELECTED_BLOCK_SEARCH_PARAM, options.selectedBlockLabel);
      navigate(
        {
          pathname: location.pathname,
          search: toReadableSearch(params),
          hash: location.hash,
        },
        {
          replace: true,
          state:
            "state" in options
              ? options.state
              : liveLocationState(location.search, location.state),
        },
      );
    },
    [
      restoreExpandedPane,
      updatePanes,
      location.search,
      location.pathname,
      location.hash,
      location.state,
      navigate,
    ],
  );

  const togglePane = useCallback(
    (id: StudioPaneId) => applyPanes((current) => togglePaneIn(current, id)),
    [applyPanes],
  );
  const openPane = useCallback(
    (id: StudioPaneId, options?: ApplyPanesOptions) =>
      applyPanes((current) => withPaneOpen(current, id), options),
    [applyPanes],
  );
  const closePane = useCallback(
    (id: StudioPaneId) => applyPanes((current) => withPaneClosed(current, id)),
    [applyPanes],
  );
  const setOpenPanes = useCallback(
    (panes: readonly StudioPaneId[]) => applyPanes(() => [...panes]),
    [applyPanes],
  );
  const setPanesOrder = useCallback(
    (order: readonly StudioPaneId[]) =>
      applyPanes((current) => {
        const next = order.filter(
          (id, index) => current.includes(id) && order.indexOf(id) === index,
        );
        return [...next, ...current.filter((id) => !next.includes(id))];
      }),
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
    preserveNextEntry,
  };
}
