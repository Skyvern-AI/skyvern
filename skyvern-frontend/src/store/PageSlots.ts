import { createContext, useContext } from "react";

export type WorkflowAnalyticsPanelProps = {
  workflowPermanentId: string;
};
export type WorkflowRunMilestoneCardProps = Readonly<{
  workflowRunId: string;
  rerun?: Readonly<{ to: string; state?: unknown }>;
}>;

export type PageSlots = {
  workflowAnalyticsPanel?: React.ComponentType<WorkflowAnalyticsPanelProps>;
  workflowRunsFilterControls?: React.ComponentType;
  workflowRunMilestoneCard?: React.ComponentType<WorkflowRunMilestoneCardProps>;
};

const PageSlotsContext = createContext<PageSlots>({});

export const PageSlotsProvider = PageSlotsContext.Provider;

export function usePageSlots(): PageSlots {
  return useContext(PageSlotsContext);
}
