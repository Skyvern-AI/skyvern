import { type PathMatch, useMatch } from "react-router-dom";

/**
 * The short run URL (/runs/{wr}) renders the studio run view, so route
 * classifiers that recognize /agents/.../studio must also treat it as a studio
 * surface. Task runs keep their own detail view.
 */
export function useStudioRunRouteMatch(): PathMatch<string> | null {
  const match = useMatch("/runs/:runId/*");
  return match?.params.runId?.startsWith("wr_") ? match : null;
}
