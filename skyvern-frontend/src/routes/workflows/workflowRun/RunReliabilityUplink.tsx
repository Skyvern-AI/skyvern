import { Link } from "react-router-dom";

import { useWorkflowReliabilityQuery } from "../hooks/useWorkflowReliabilityQuery";
import { useWorkflowRunWithWorkflowQuery } from "../hooks/useWorkflowRunWithWorkflowQuery";
import { reliabilityHasActivity } from "./reliabilityStatus";

type Props = {
  workflowRunId?: string;
};

function RunReliabilityUplink({ workflowRunId }: Props) {
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery({
    workflowRunId,
  });
  const workflowPermanentId = workflowRun?.workflow?.workflow_permanent_id;
  const { data: reliability } = useWorkflowReliabilityQuery({
    workflowPermanentId,
    enabled: Boolean(workflowPermanentId),
  });

  if (
    !workflowPermanentId ||
    !reliability ||
    !reliabilityHasActivity(reliability) ||
    !reliability.scored ||
    reliability.state === "healthy"
  ) {
    return null;
  }

  const story =
    reliability.healed_runs > 0
      ? `This workflow self-healed ${reliability.healed_runs} of the last ${reliability.window_runs} runs`
      : `This workflow fell back to a backup on ${reliability.floor_runs} of the last ${reliability.window_runs} runs`;

  return (
    <Link
      to={`/agents/${workflowPermanentId}/runs`}
      className="inline-flex w-fit items-center rounded-md border border-border bg-slate-elevation2/80 px-2 py-0.5 text-xs text-warning backdrop-blur-sm hover:underline"
    >
      {story} →
    </Link>
  );
}

export { RunReliabilityUplink };
