import { type PathMatch, useMatch } from "react-router-dom";

/**
 * Matches an /agents sub-path, also honoring the legacy /workflows alias.
 * The alias arm only fires in ancestors that render during the redirect's
 * in-flight frame (RootLayout, DebugStoreProvider) — it keeps the sidebar
 * from flashing on old bookmarks. Components mounted inside the /agents
 * subtree never see a /workflows URL.
 */
function useAgentsPathMatch(subPattern: string): PathMatch<string> | null {
  const agentsMatch = useMatch(`/agents${subPattern}`);
  const legacyMatch = useMatch(`/workflows${subPattern}`);
  return agentsMatch ?? legacyMatch;
}

export { useAgentsPathMatch };
