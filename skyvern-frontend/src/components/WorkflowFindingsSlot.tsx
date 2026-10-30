import type { WorkflowReliability } from "@/routes/workflows/types/reliabilityTypes";
import { WorkflowReliabilityBadge } from "@/routes/workflows/WorkflowReliabilityBadge";
import type { ReactNode } from "react";
import { usePageSlots } from "@/store/PageSlots";

function WorkflowFindingsBoundary({
  workflowPermanentIds,
  children,
}: Readonly<{ workflowPermanentIds: readonly string[]; children: ReactNode }>) {
  const { workflowFindingsScope: Scope } = usePageSlots();
  return Scope ? (
    <Scope workflowPermanentIds={workflowPermanentIds}>{children}</Scope>
  ) : (
    <>{children}</>
  );
}

function WorkflowFindingsButton(
  props: Readonly<{
    workflowPermanentId: string;
    title?: string;
    surface: "list" | "editor";
    reliability?: WorkflowReliability;
  }>,
) {
  const { workflowFindingsTrigger: Trigger } = usePageSlots();
  return Trigger ? (
    <Trigger {...props} />
  ) : (
    <WorkflowReliabilityBadge reliability={props.reliability} />
  );
}

export { WorkflowFindingsBoundary, WorkflowFindingsButton };
