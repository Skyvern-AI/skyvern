import { useFeatureFlag } from "./useFeatureFlag";

export function useWorkflowRunViewingV2(): boolean {
  return useFeatureFlag("workflow_run_viewing_v2") ?? false;
}
