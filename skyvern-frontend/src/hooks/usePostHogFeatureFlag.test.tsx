import { act, renderHook } from "@testing-library/react";
import { useState, type PropsWithChildren } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const posthogMock = vi.hoisted(() => ({
  getFeatureFlag: vi.fn(),
  isFeatureEnabled: vi.fn(),
  onFeatureFlags: vi.fn(),
}));

vi.mock("posthog-js", () => ({ default: posthogMock }));

import {
  PostHogInitializedContext,
  usePostHogFeatureFlagEnabled,
  usePostHogFeatureFlagVariantKey,
} from "./usePostHogFeatureFlag";

describe("PostHog feature flag hooks", () => {
  beforeEach(() => {
    posthogMock.getFeatureFlag.mockReset();
    posthogMock.isFeatureEnabled.mockReset();
    posthogMock.onFeatureFlags.mockReset().mockReturnValue(vi.fn());
  });

  it("subscribes after initialization and updates from feature-flag events", () => {
    posthogMock.isFeatureEnabled.mockReturnValue(true);
    posthogMock.getFeatureFlag.mockReturnValue("blue");

    let setInitialized!: (initialized: boolean) => void;
    function Wrapper({ children }: PropsWithChildren) {
      const [initialized, setValue] = useState(false);
      setInitialized = setValue;
      return (
        <PostHogInitializedContext.Provider value={initialized}>
          {children}
        </PostHogInitializedContext.Provider>
      );
    }

    const { result } = renderHook(
      () => ({
        enabled: usePostHogFeatureFlagEnabled("enabled-flag"),
        variant: usePostHogFeatureFlagVariantKey("variant-flag"),
      }),
      { wrapper: Wrapper },
    );

    expect(result.current).toEqual({ enabled: undefined, variant: undefined });
    expect(posthogMock.onFeatureFlags).not.toHaveBeenCalled();

    act(() => setInitialized(true));

    expect(result.current).toEqual({ enabled: true, variant: "blue" });
    expect(posthogMock.onFeatureFlags).toHaveBeenCalledTimes(2);

    posthogMock.isFeatureEnabled.mockReturnValue(false);
    posthogMock.getFeatureFlag.mockReturnValue("green");
    const eventCallbacks = posthogMock.onFeatureFlags.mock.calls.map(
      ([callback]) => callback,
    );
    act(() => eventCallbacks.forEach((callback) => callback([], {}, {})));

    expect(result.current).toEqual({ enabled: false, variant: "green" });
  });

  it("uses the initialized PostHog client when no OSS initialization context is provided", () => {
    posthogMock.isFeatureEnabled.mockReturnValue(true);

    const { result } = renderHook(() =>
      usePostHogFeatureFlagEnabled("enabled-flag"),
    );

    expect(result.current).toBe(true);
    expect(posthogMock.onFeatureFlags).toHaveBeenCalledTimes(1);
  });
});
