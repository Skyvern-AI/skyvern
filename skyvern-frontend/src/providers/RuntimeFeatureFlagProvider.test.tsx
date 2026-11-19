import { renderHook } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { useRuntimeConfig } from "@/hooks/useRuntimeConfig";
import { useWorkflowTaggingEnabled } from "@/hooks/useWorkflowTaggingEnabled";
import { RuntimeFeatureFlagProvider } from "./RuntimeFeatureFlagProvider";

vi.mock("@/hooks/useRuntimeConfig", () => ({
  useRuntimeConfig: vi.fn(),
}));

const useRuntimeConfigMock = vi.mocked(useRuntimeConfig);

function wrapper({ children }: PropsWithChildren) {
  return <RuntimeFeatureFlagProvider>{children}</RuntimeFeatureFlagProvider>;
}

describe("RuntimeFeatureFlagProvider", () => {
  beforeEach(() => {
    useRuntimeConfigMock.mockReturnValue({
      data: {
        code_block_access: true,
      },
    } as ReturnType<typeof useRuntimeConfig>);
  });

  it("exposes the OSS runtime value for CODE_BLOCK_ACCESS", () => {
    const { result } = renderHook(() => useFeatureFlag("CODE_BLOCK_ACCESS"), {
      wrapper,
    });

    expect(result.current).toBe(true);
  });

  it("keeps code authoring unavailable when runtime execution access is disabled", () => {
    useRuntimeConfigMock.mockReturnValue({
      data: {
        code_block_access: false,
      },
    } as ReturnType<typeof useRuntimeConfig>);

    const { result } = renderHook(() => useFeatureFlag("CODE_BLOCK_ACCESS"), {
      wrapper,
    });

    expect(result.current).toBe(false);
  });

  it("keeps workflow tagging on, since the OSS backend never gates it", () => {
    useRuntimeConfigMock.mockReturnValue({
      data: undefined,
    } as ReturnType<typeof useRuntimeConfig>);

    const { result } = renderHook(() => useWorkflowTaggingEnabled(), {
      wrapper,
    });

    expect(result.current).toBe(true);
  });

  it("leaves unrelated cloud feature flags unresolved", () => {
    const { result } = renderHook(() => useFeatureFlag("SOME_CLOUD_FLAG"), {
      wrapper,
    });

    expect(result.current).toBeUndefined();
  });

  it("leaves runtime flags unresolved while config is loading", () => {
    useRuntimeConfigMock.mockReturnValue({
      data: undefined,
    } as ReturnType<typeof useRuntimeConfig>);

    const { result } = renderHook(() => useFeatureFlag("CODE_BLOCK_ACCESS"), {
      wrapper,
    });

    expect(result.current).toBeUndefined();
  });
});
