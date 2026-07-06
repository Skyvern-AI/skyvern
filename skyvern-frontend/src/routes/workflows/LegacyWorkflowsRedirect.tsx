import { Navigate, useLocation } from "react-router-dom";

/**
 * Permanent alias: pre-rename /workflows URLs (bookmarks, webhooks, docs) land
 * on /agents. Swaps the prefix on the raw pathname — no decode/re-encode — so
 * the rest of the path, query, hash, and state pass through byte-for-byte.
 */
function LegacyWorkflowsRedirect() {
  const location = useLocation();
  return (
    <Navigate
      to={
        location.pathname.replace(/^\/workflows(?=\/|$)/, "/agents") +
        location.search +
        location.hash
      }
      state={location.state}
      replace
    />
  );
}

export { LegacyWorkflowsRedirect };
