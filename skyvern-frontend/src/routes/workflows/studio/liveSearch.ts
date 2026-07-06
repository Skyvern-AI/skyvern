// window.location is blank under a memory router (tests); the router's
// location is identical to the live URL in a real browser. Reading the live
// URL matters because pushState is synchronous while router state is not.
export function liveSearch(routerSearch: string): string {
  return window.location.search || routerSearch;
}
