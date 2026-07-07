import type { WorkflowBlockType } from "../../types/workflowTypes";
import type { DebugSessionRun } from "../../hooks/useDebugSessionRunsQuery";

export type RecentActivityViewProps = {
  /** Debug-session runs, ascending by `created_at`; list views may reverse a clone. */
  runs: Array<DebugSessionRun>;
  currentActivityKey: string | null;
  isWorkflowRunning: boolean;
  blockTypeByLabel: Map<string, WorkflowBlockType>;
  now: number;
  onSelect: (run: DebugSessionRun) => void;
};
