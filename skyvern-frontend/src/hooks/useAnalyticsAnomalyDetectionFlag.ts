import { usePostHogFeatureFlagEnabled as useFeatureFlagEnabled } from "@/hooks/usePostHogFeatureFlag";
import { ANALYTICS_ANOMALY_DETECTION_FLAG } from "@/util/featureFlags";

// VITE_MOCK_ANALYTICS fakes API responses (see cloud/dev/mockAnalyticsServer.ts) but
// mock-mode local dev can't reach PostHog, so gated surfaces would stay hidden.
export function useAnalyticsAnomalyDetectionFlag(): boolean | undefined {
  const enabled = useFeatureFlagEnabled(ANALYTICS_ANOMALY_DETECTION_FLAG);
  if (import.meta.env.DEV && import.meta.env.VITE_MOCK_ANALYTICS === "1") {
    return true;
  }
  return enabled;
}
