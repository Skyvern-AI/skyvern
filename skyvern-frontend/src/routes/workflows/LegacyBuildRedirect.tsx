import { Navigate, useLocation, useParams } from "react-router-dom";

/**
 * Legacy /build and /debug URLs land on /studio; block-run URLs carry the run as
 * ?wr=&bl= so the Run tab opens on it in block-scoped mode.
 */
function LegacyBuildRedirect() {
  const { workflowPermanentId, workflowRunId, blockLabel } = useParams();
  const location = useLocation();
  // Block-run URLs carry the run as ?wr=&bl= (bl marks block scope); plain /build
  // keeps its own search (e.g. ?via= onboarding telemetry) rather than dropping it.
  const query =
    workflowRunId && blockLabel
      ? `?wr=${workflowRunId}&bl=${encodeURIComponent(blockLabel)}`
      : location.search;
  return (
    <Navigate
      to={`/workflows/${workflowPermanentId}/studio${query}`}
      state={location.state}
      replace
    />
  );
}

export { LegacyBuildRedirect };
