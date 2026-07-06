import { createContext, useContext } from "react";

export type WorkflowAnalyticsPanelProps = {
  workflowPermanentId: string;
};

export type PageSlots = {
  workflowAnalyticsPanel?: React.ComponentType<WorkflowAnalyticsPanelProps>;
  workflowRunsFilterControls?: React.ComponentType;
};

const PageSlotsContext = createContext<PageSlots>({});

export const PageSlotsProvider = PageSlotsContext.Provider;

export function usePageSlots(): PageSlots {
  return useContext(PageSlotsContext);
}
