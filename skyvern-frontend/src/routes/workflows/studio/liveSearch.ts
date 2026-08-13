// window.location is blank under a memory router (tests); the router's
// location is identical to the live URL in a real browser. Reading the live
// URL matters because pushState is synchronous while router state is not.
export function liveSearch(routerSearch: string): string {
  return window.location.search || routerSearch;
}

// React Router stores the current location state under `history.state.usr`.
// Follow the same source choice as liveSearch so one pane write cannot mix a
// freshly-pushed browser search with state from the previous render.
export function liveLocationState(
  routerSearch: string,
  routerState: unknown,
): unknown {
  if (!window.location.search || window.location.search === routerSearch) {
    return routerState ?? null;
  }
  const historyState = window.history.state;
  if (
    typeof historyState === "object" &&
    historyState !== null &&
    "usr" in historyState
  ) {
    return historyState.usr ?? null;
  }
  return null;
}
