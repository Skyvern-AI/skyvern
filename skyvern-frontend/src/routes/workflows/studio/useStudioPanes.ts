import { useCallback, useMemo } from "react";
import {
  useLocation,
  useNavigate,
  type NavigateOptions,
} from "react-router-dom";

import {
  resolveOpenPanes,
  searchWithPanes,
  togglePane as togglePaneIn,
  withPaneClosed,
  withPaneOpen,
  type StudioPaneId,
} from "./panes";

/**
 * Pane state lives in the URL (?panes=), so the open set and its order are
 * shareable and can never drift from navigation. Writes merge against the live
 * URL rather than this render's closure: pushState is synchronous, so a
 * concurrent navigate() (e.g. a block-run launch) is already visible there.
 */
export function useStudioPanes() {
  const location = useLocation();
  const navigate = useNavigate();

  const panes = useMemo(
    () => resolveOpenPanes(location.search),
    [location.search],
  );

  const applyPanes = useCallback(
    (
      compute: (current: StudioPaneId[]) => StudioPaneId[],
      options?: Pick<NavigateOptions, "state">,
    ) => {
      // window.location is blank under a memory router (tests); fall back to
      // the router's location, which is identical to the live URL in a browser.
      const liveSearch = window.location.search || location.search;
      const next = compute(resolveOpenPanes(liveSearch));
      navigate(
        { search: searchWithPanes(liveSearch, next) },
        { replace: true, ...options },
      );
    },
    [navigate, location.search],
  );

  const togglePane = useCallback(
    (id: StudioPaneId) => applyPanes((current) => togglePaneIn(current, id)),
    [applyPanes],
  );

  const openPane = useCallback(
    (id: StudioPaneId, options?: Pick<NavigateOptions, "state">) =>
      applyPanes((current) => withPaneOpen(current, id), options),
    [applyPanes],
  );

  const closePane = useCallback(
    (id: StudioPaneId) => applyPanes((current) => withPaneClosed(current, id)),
    [applyPanes],
  );

  return { panes, togglePane, openPane, closePane };
}
