import { type PathMatch, useMatch } from "react-router-dom";

/**
 * The short run URL (/runs/{wr}) renders the studio run view when the preview is
 * on, so route classifiers that recognize /agents/.../studio must also treat it
 * as a studio surface. Only workflow runs (wr_) route into the studio; task runs
 * keep their own detail view. Callers still gate on the studio flag themselves,
 * since flag-off /runs/{wr} renders the legacy run page.
 */
export function useStudioRunRouteMatch(): PathMatch<string> | null {
  const match = useMatch("/runs/:runId/*");
  return match?.params.runId?.startsWith("wr_") ? match : null;
}
