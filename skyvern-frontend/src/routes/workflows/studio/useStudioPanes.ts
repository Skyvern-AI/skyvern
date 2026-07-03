import { useCallback, useMemo } from "react";
import {
  useLocation,
  useNavigate,
  type NavigateOptions,
} from "react-router-dom";

import { liveSearch } from "./liveSearch";
import {
  panesListEqual,
  resolveOpenPanes,
  searchWithPanes,
  togglePane as togglePaneIn,
  withPaneClosed,
  withPaneOpen,
  type StudioPaneId,
} from "./panes";
import { useStudioPaneDefaults } from "./StudioPaneDefaultsContext";

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

  // The mount-time viewport clamp only masks the exact pane list the URL
  // carried at mount; any other list means someone navigated, so present it
  // as-is. The first write clears the clamp for good.
  const present = useCallback(
    (resolved: StudioPaneId[]): StudioPaneId[] =>
      clamp && panesListEqual(resolved, clamp.source)
        ? [...clamp.presented]
        : resolved,
    [clamp],
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
      options?: Pick<NavigateOptions, "state">,
    ) => {
      const search = liveSearch(location.search);
      const current = resolveLivePanes();
      const next = compute(current);
      notePaneWrite({ previous: current, next });
      navigate(
        { search: searchWithPanes(search, next) },
        { replace: true, ...options },
      );
    },
    [navigate, location.search, resolveLivePanes, notePaneWrite],
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

  return { panes, resolveLivePanes, togglePane, openPane, closePane };
}
