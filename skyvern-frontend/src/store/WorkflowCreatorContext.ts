import { createContext, useContext } from "react";

import { usePageSlots } from "./PageSlots";

type ResolveMemberName = (userId: string) => string | null;

type WorkflowCreatorDirectory = {
  resolveMemberName: ResolveMemberName;
  // While false, an id that does not resolve is one we have not loaded yet, not one with no member
  // behind it. Deployments without a directory are settled from the start: they resolve nothing.
  isSettled: boolean;
};

const WorkflowCreatorContext = createContext<WorkflowCreatorDirectory>({
  resolveMemberName: () => null,
  isSettled: true,
});

export const WorkflowCreatorProvider = WorkflowCreatorContext.Provider;

export function useWorkflowCreatorDirectory(): WorkflowCreatorDirectory {
  return useContext(WorkflowCreatorContext);
}

// Only deployments that register a creator directory can resolve creators to names.
export function useCreatorColumnEnabled(): boolean {
  return Boolean(usePageSlots().workflowCreatorDirectory);
}
