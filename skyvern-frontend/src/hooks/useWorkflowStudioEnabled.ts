import { useFeatureFlagEnabled } from "posthog-js/react";

import { WORKFLOW_STUDIO_FLAG } from "@/util/featureFlags";

// Client-side eval so the flag reflects per-user opt-in (PostHog person property),
// not the per-org server-evaluated path. Gates the whole studio redesign.
// undefined means flags have not resolved yet — callers that act irreversibly on
// "off" (e.g. URL rewrites) must distinguish it from a resolved false.
export function useWorkflowStudioFlagState(): boolean | undefined {
  return useFeatureFlagEnabled(WORKFLOW_STUDIO_FLAG);
}

export function useWorkflowStudioEnabled(): boolean {
  return useWorkflowStudioFlagState() ?? false;
}
