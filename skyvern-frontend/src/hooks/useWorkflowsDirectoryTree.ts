import { useFeatureFlagEnabled } from "posthog-js/react";

import { WORKFLOWS_DIRECTORY_TREE_FLAG } from "@/util/featureFlags";

// Read client-side so the flag reflects per-user opt-in (a PostHog person
// property) rather than the per-org server-evaluated path. Absent flag -> false,
// which keeps the default flat folders/list experience.
export function useWorkflowsDirectoryTree(): boolean {
  return useFeatureFlagEnabled(WORKFLOWS_DIRECTORY_TREE_FLAG) ?? false;
}
