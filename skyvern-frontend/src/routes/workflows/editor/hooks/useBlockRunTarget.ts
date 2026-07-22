import { useParams, useSearchParams } from "react-router-dom";

import { useFirstParam } from "@/hooks/useFirstParam";

/**
 * The block run the editor canvas is focused on. The legacy debugger carries
 * the run and target block in the path (/:workflowRunId/:blockLabel/build); the
 * short /runs/{wr} URL carries the run as the runId path param; the studio
 * carries them in ?wr= / ?bl= so the canvas never re-mounts on a run start.
 * Block components must gate on whichever shape is present (matching the run-id
 * resolution in useStudioRunId), or the running chip / stop button /
 * play-disable go inert.
 */
export function useBlockRunTarget(): {
  workflowRunId: string | undefined;
  blockLabel: string | undefined;
} {
  const { blockLabel } = useParams();
  const [searchParams] = useSearchParams();
  const urlRunId = useFirstParam("workflowRunId", "runId");
  return {
    workflowRunId: searchParams.get("wr") ?? urlRunId,
    blockLabel: blockLabel ?? searchParams.get("bl") ?? undefined,
  };
}
