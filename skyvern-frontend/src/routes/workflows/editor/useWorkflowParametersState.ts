import { useContext } from "react";
import { WorkflowParametersStateContext } from "./WorkflowParametersStateContext";

function useWorkflowParametersState() {
  const value = useContext(WorkflowParametersStateContext);
  if (value === undefined) {
    throw new Error(
      "useWorkflowParametersState must be used within a WorkflowParametersStateProvider",
    );
  }
  return value;
}

export { useWorkflowParametersState };
