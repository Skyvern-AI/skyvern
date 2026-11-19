import { createContext } from "react";

// Reads a run's agent step calls when its card mounts or reopens. Skipped while that run's poll is live or
// in flight, or when the card has its calls and the last read showed every agent step finished.
export const AgentStepCallsLoaderContext = createContext<
  ((workflowRunId: string, cardHasCalls: boolean) => void) | null
>(null);
