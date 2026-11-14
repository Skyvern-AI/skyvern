import { useCallback } from "react";

import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";

export function useToggleHistoryPanel(): () => void {
  const workflowPanelState = useWorkflowPanelStore((s) => s.workflowPanelState);
  const setWorkflowPanelState = useWorkflowPanelStore(
    (s) => s.setWorkflowPanelState,
  );

  return useCallback(() => {
    const wasInComparisonMode = workflowPanelState.data?.showComparison;
    const isHistoryPanelOpen =
      workflowPanelState.active && workflowPanelState.content === "history";

    const active = !(wasInComparisonMode || isHistoryPanelOpen);
    setWorkflowPanelState({
      active,
      content: "history",
      data: {
        showComparison: false,
        version1: undefined,
        version2: undefined,
      },
    });
  }, [workflowPanelState, setWorkflowPanelState]);
}
