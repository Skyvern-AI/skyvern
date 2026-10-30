import type { WorkflowReliability } from "./types/reliabilityTypes";
import {
  reliabilityLabel,
  reliabilityShowsState,
} from "./workflowRun/reliabilityStatus";

type Props = {
  reliability?: WorkflowReliability;
  actionableFindingCount?: number;
};

function WorkflowReliabilityBadge({
  reliability,
  actionableFindingCount = 0,
}: Props) {
  if (
    actionableFindingCount === 0 &&
    (!reliability ||
      !reliabilityShowsState(reliability) ||
      reliability.state !== "action_needed")
  ) {
    return null;
  }

  const reliabilityNeedsAttention =
    !!reliability &&
    reliabilityShowsState(reliability) &&
    reliability.state === "action_needed";
  const reason =
    actionableFindingCount > 0
      ? `Unresolved findings${reliabilityNeedsAttention ? " and workflow reliability concerns" : ""}`
      : "Workflow reliability concerns";
  return (
    <span
      title={reason}
      className="inline-flex shrink-0 items-center whitespace-nowrap rounded border border-border bg-warning/15 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-warning"
    >
      {reliabilityLabel("action_needed")}
    </span>
  );
}

export { WorkflowReliabilityBadge };
