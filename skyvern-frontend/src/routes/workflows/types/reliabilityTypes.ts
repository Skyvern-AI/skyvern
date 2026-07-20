type WorkflowReliabilityState = "healthy" | "watch" | "action_needed";

type WorkflowReliability = {
  state: WorkflowReliabilityState;
  outcome_risk: boolean;
  scored: boolean;
  window_runs: number;
  healed_runs: number;
  heal_rate: number;
  consecutive_healed_runs: number;
  floor_runs: number;
  outcome_risk_runs: number;
};

type WorkflowsReliabilityBatchResponse = {
  reliabilities: Record<string, WorkflowReliability>;
};

export type {
  WorkflowReliability,
  WorkflowReliabilityState,
  WorkflowsReliabilityBatchResponse,
};
