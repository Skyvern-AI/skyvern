import { useFeatureFlagEnabled } from "posthog-js/react";

// Read client-side so the flag reflects per-user opt-in (a PostHog person
// property) rather than the per-org server-evaluated path.
export function useWorkflowRunViewingV2(): boolean {
  return useFeatureFlagEnabled("workflow_run_viewing_v2") ?? false;
}
