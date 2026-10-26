// @vitest-environment jsdom
import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { mockEnabled } = vi.hoisted(() => ({ mockEnabled: vi.fn() }));

vi.mock("posthog-js/react", () => ({
  useFeatureFlagEnabled: () => mockEnabled(),
}));
vi.mock("@/hooks/usePostHogFeatureFlag", () => ({
  usePostHogFeatureFlagEnabled: () => mockEnabled(),
}));

import { createElement, type ReactNode } from "react";
import { FeatureFlagContext } from "@/hooks/useFeatureFlag";
import { useAnalyticsDashboardFlag } from "./useAnalyticsDashboardFlag";

const withServerFlag =
  (value: boolean | undefined) =>
  ({ children }: { children: ReactNode }) =>
    createElement(
      FeatureFlagContext.Provider,
      { value: () => value },
      children,
    );

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

  it("is on when the server enables the flag, even before or without PostHog flags", () => {
    mockEnabled.mockReturnValue(undefined);
    expect(
      renderHook(() => useAnalyticsDashboardFlag(), {
        wrapper: withServerFlag(true),
      }).result.current,
    ).toBe(true);

    mockEnabled.mockReturnValue(false);
    expect(
      renderHook(() => useAnalyticsDashboardFlag(), {
        wrapper: withServerFlag(true),
      }).result.current,
    ).toBe(true);
  });

  it("leaves the PostHog value in charge when the server flag is off or unknown", () => {
    for (const server of [false, undefined]) {
      for (const posthog of [true, false, undefined]) {
        mockEnabled.mockReturnValue(posthog);
        expect(
          renderHook(() => useAnalyticsDashboardFlag(), {
            wrapper: withServerFlag(server),
          }).result.current,
        ).toBe(posthog);
      }
    }
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
