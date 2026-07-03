import { useFeatureFlagEnabled } from "posthog-js/react";
import { ANALYTICS_DASHBOARD_FLAG } from "@/util/featureFlags";

// VITE_MOCK_ANALYTICS fakes API responses (see cloud/dev/mockAnalyticsServer.ts)
// but can't reach real PostHog, so every ANALYTICS_DASHBOARD-gated surface
// would stay hidden in local dev without this override.
export function useAnalyticsDashboardFlag(): boolean | undefined {
  const enabled = useFeatureFlagEnabled(ANALYTICS_DASHBOARD_FLAG);
  if (import.meta.env.DEV && import.meta.env.VITE_MOCK_ANALYTICS === "1") {
    return true;
  }
  return enabled;
}
