import { useSearchParams } from "react-router-dom";

import { useFirstParam } from "@/hooks/useFirstParam";

/**
 * The run the studio shell is focused on. Shell-started runs carry the id in ?wr=
 * (path params would re-layout the canvas); legacy routes carry it as a path param.
 */
export function useStudioRunId(): string | undefined {
  const [searchParams] = useSearchParams();
  const urlRunId = useFirstParam("workflowRunId", "runId");
  return searchParams.get("wr") ?? urlRunId;
}
