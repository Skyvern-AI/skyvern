import { useCallback } from "react";

import { useShowAllCodeStore } from "@/store/ShowAllCodeStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";

export function useToggleCodeView(): () => void {
  const workflowPanelState = useWorkflowPanelStore((s) => s.workflowPanelState);
  const setWorkflowPanelState = useWorkflowPanelStore(
    (s) => s.setWorkflowPanelState,
  );
  const showAllCode = useShowAllCodeStore((s) => s.showAllCode);
  const setShowAllCode = useShowAllCodeStore((s) => s.setShowAllCode);

  return useCallback(() => {
    const wasInComparisonMode = workflowPanelState.data?.showComparison;

    setWorkflowPanelState({
      active: false,
      content: "history",
      data: {
        showComparison: false,
        version1: undefined,
        version2: undefined,
      },
    });

    if (wasInComparisonMode) {
      setShowAllCode(true);
    } else {
      setShowAllCode(!showAllCode);
    }
  }, [workflowPanelState, setWorkflowPanelState, showAllCode, setShowAllCode]);
}
