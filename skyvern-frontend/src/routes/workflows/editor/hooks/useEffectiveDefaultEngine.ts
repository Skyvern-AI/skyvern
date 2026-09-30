import type { RunEngine } from "@/api/types";
import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";
import { useWorkflowQuery } from "@/routes/workflows/hooks/useWorkflowQuery";

function useEffectiveDefaultEngine(): RunEngine | null {
  const workflowPermanentId = useWorkflowPermanentId();
  const { data: workflow } = useWorkflowQuery({ workflowPermanentId });
  return workflow?.effective_default_engine ?? null;
}

export { useEffectiveDefaultEngine };
