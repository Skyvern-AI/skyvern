import { useFeatureFlagEnabled } from "posthog-js/react";
import { ANALYTICS_ANOMALY_DETECTION_FLAG } from "@/util/featureFlags";

// Same VITE_MOCK_ANALYTICS override as useAnalyticsDashboardFlag: mock-mode
// local dev can't reach PostHog, so gated surfaces would stay hidden.
export function useAnalyticsAnomalyDetectionFlag(): boolean | undefined {
  const enabled = useFeatureFlagEnabled(ANALYTICS_ANOMALY_DETECTION_FLAG);
  if (import.meta.env.DEV && import.meta.env.VITE_MOCK_ANALYTICS === "1") {
    return true;
  }
  return enabled;
}
