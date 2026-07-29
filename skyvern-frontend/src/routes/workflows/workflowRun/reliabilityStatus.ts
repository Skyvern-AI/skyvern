import type {
  WorkflowReliability,
  WorkflowReliabilityState,
} from "../types/reliabilityTypes";

type ReliabilityTone = "neutral" | "amber";

const reliabilityLabels: Record<WorkflowReliabilityState, string> = {
  healthy: "Healthy",
  watch: "Watch",
  action_needed: "Needs a look",
};

function reliabilityHasActivity(reliability: WorkflowReliability): boolean {
  return reliability.healed_runs > 0 || reliability.floor_runs > 0;
}

function reliabilityShowsState(reliability: WorkflowReliability): boolean {
  return reliability.scored;
}

function reliabilityLabel(state: WorkflowReliabilityState): string {
  return reliabilityLabels[state];
}

function reliabilityTone(state: WorkflowReliabilityState): ReliabilityTone {
  if (state === "healthy") {
    return "neutral";
  }
  return "amber";
}

export {
  reliabilityHasActivity,
  reliabilityLabel,
  reliabilityShowsState,
  reliabilityTone,
};
export type { ReliabilityTone };
