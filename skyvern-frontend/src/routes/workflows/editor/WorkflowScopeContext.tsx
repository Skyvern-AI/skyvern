import { createContext, useContext } from "react";

type WorkflowScopeValue = {
  workflowId: string | null;
  // True when FlowRenderer is mounted as a comparison/diff canvas. Node
  // subtrees can read this via `useWorkflowScopeReadOnly()` to gate any
  // user-driven mutation (selection, collapse toggle, etc.) that would
  // otherwise persist to the editor's stores while the user is only
  // reviewing a workflow version.
  readOnly: boolean;
};

export const WorkflowScopeContext = createContext<WorkflowScopeValue>({
  workflowId: null,
  readOnly: false,
});

export function useWorkflowScopeId(): string | null {
  return useContext(WorkflowScopeContext).workflowId;
}

export function useWorkflowScopeReadOnly(): boolean {
  return useContext(WorkflowScopeContext).readOnly;
}
