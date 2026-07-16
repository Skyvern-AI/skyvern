import type { WorkflowReliability } from "./types/reliabilityTypes";
import {
  reliabilityLabel,
  reliabilityShowsState,
} from "./workflowRun/reliabilityStatus";

type Props = {
  reliability?: WorkflowReliability;
};

function WorkflowReliabilityBadge({ reliability }: Props) {
  if (
    !reliability ||
    !reliabilityShowsState(reliability) ||
    reliability.state !== "action_needed"
  ) {
    return null;
  }

  return (
    <span className="inline-flex shrink-0 items-center whitespace-nowrap rounded border border-border bg-warning/15 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-warning">
      {reliabilityLabel(reliability.state)}
    </span>
  );
}

export { WorkflowReliabilityBadge };
