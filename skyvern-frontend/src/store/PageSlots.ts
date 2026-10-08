import { createContext, useContext } from "react";

export type WorkflowAnalyticsPanelProps = {
  workflowPermanentId: string;
};
export type WorkflowRunMilestoneCardProps = Readonly<{
  workflowRunId: string;
  rerun?: Readonly<{ to: string; state?: unknown }>;
}>;

export type FirstRunWaitCardProps = Readonly<{
  phase: "run_provisioning" | "debug_browser_warming";
}>;

export type PageSlots = {
  workflowAnalyticsPanel?: React.ComponentType<WorkflowAnalyticsPanelProps>;
  workflowRunsFilterControls?: React.ComponentType;
  workflowRunMilestoneCard?: React.ComponentType<WorkflowRunMilestoneCardProps>;
  firstRunWaitCard?: React.ComponentType<FirstRunWaitCardProps>;
  workflowCreatorDirectory?: React.ComponentType<{
    children: React.ReactNode;
  }>;
};

const PageSlotsContext = createContext<PageSlots>({});

export const PageSlotsProvider = PageSlotsContext.Provider;

export function usePageSlots(): PageSlots {
  return useContext(PageSlotsContext);
}
