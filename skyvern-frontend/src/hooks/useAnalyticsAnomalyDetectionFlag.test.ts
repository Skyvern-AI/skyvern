// @vitest-environment jsdom
import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { mockEnabled } = vi.hoisted(() => ({ mockEnabled: vi.fn() }));

vi.mock("posthog-js/react", () => ({
  useFeatureFlagEnabled: () => mockEnabled(),
}));

import { useAnalyticsAnomalyDetectionFlag } from "./useAnalyticsAnomalyDetectionFlag";

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllEnvs();
});

describe("useAnalyticsAnomalyDetectionFlag", () => {
  it("passes through the posthog flag value when mock analytics is off", () => {
    mockEnabled.mockReturnValue(false);
    expect(
      renderHook(() => useAnalyticsAnomalyDetectionFlag()).result.current,
    ).toBe(false);

    mockEnabled.mockReturnValue(undefined);
    expect(
      renderHook(() => useAnalyticsAnomalyDetectionFlag()).result.current,
    ).toBeUndefined();

    mockEnabled.mockReturnValue(true);
    expect(
      renderHook(() => useAnalyticsAnomalyDetectionFlag()).result.current,
    ).toBe(true);
  });

  it("forces true under VITE_MOCK_ANALYTICS=1, even when the posthog flag is false or undefined", () => {
    vi.stubEnv("VITE_MOCK_ANALYTICS", "1");

    mockEnabled.mockReturnValue(false);
    expect(
      renderHook(() => useAnalyticsAnomalyDetectionFlag()).result.current,
    ).toBe(true);

    mockEnabled.mockReturnValue(undefined);
    expect(
      renderHook(() => useAnalyticsAnomalyDetectionFlag()).result.current,
    ).toBe(true);
  });
});
