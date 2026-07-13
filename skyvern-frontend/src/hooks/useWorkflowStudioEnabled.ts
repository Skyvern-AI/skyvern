import { useFeatureFlagEnabled } from "posthog-js/react";

import { WORKFLOW_STUDIO_FLAG } from "@/util/featureFlags";

// Client-side eval so the flag reflects per-user opt-in (PostHog person property),
// not the per-org server-evaluated path. Gates the whole studio redesign.
export function useWorkflowStudioEnabled(): boolean {
  return useFeatureFlagEnabled(WORKFLOW_STUDIO_FLAG) ?? false;
}
