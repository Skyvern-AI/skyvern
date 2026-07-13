// @vitest-environment jsdom
import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { mockEnabled } = vi.hoisted(() => ({ mockEnabled: vi.fn() }));

vi.mock("posthog-js/react", () => ({
  useFeatureFlagEnabled: () => mockEnabled(),
}));

import { useAnalyticsDashboardFlag } from "./useAnalyticsDashboardFlag";

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllEnvs();
});

describe("useAnalyticsDashboardFlag", () => {
  it("passes through the posthog flag value when mock analytics is off", () => {
    mockEnabled.mockReturnValue(false);
    expect(renderHook(() => useAnalyticsDashboardFlag()).result.current).toBe(
      false,
    );

    mockEnabled.mockReturnValue(undefined);
    expect(
      renderHook(() => useAnalyticsDashboardFlag()).result.current,
    ).toBeUndefined();

    mockEnabled.mockReturnValue(true);
    expect(renderHook(() => useAnalyticsDashboardFlag()).result.current).toBe(
      true,
    );
  });

  it("forces true under VITE_MOCK_ANALYTICS=1, even when the posthog flag is false or undefined", () => {
    vi.stubEnv("VITE_MOCK_ANALYTICS", "1");

    mockEnabled.mockReturnValue(false);
    expect(renderHook(() => useAnalyticsDashboardFlag()).result.current).toBe(
      true,
    );

    mockEnabled.mockReturnValue(undefined);
    expect(renderHook(() => useAnalyticsDashboardFlag()).result.current).toBe(
      true,
    );
  });
});
