import { useParams, useSearchParams } from "react-router-dom";

/**
 * The block run the editor canvas is focused on. The legacy debugger carries
 * the run and target block in the path (/:workflowRunId/:blockLabel/build);
 * the studio carries them in ?wr= / ?bl= so the canvas never re-mounts on a
 * run start. Block components must gate on whichever shape is present, or the
 * running chip / stop button / play-disable go inert in the studio.
 */
export function useBlockRunTarget(): {
  workflowRunId: string | undefined;
  blockLabel: string | undefined;
} {
  const { blockLabel, workflowRunId } = useParams();
  const [searchParams] = useSearchParams();
  return {
    workflowRunId: workflowRunId ?? searchParams.get("wr") ?? undefined,
    blockLabel: blockLabel ?? searchParams.get("bl") ?? undefined,
  };
}
