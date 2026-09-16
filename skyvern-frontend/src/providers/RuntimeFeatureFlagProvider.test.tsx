import { renderHook } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { useRuntimeConfig } from "@/hooks/useRuntimeConfig";
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
        workflow_copilot_code_block_mode: true,
        code_block_access: true,
      },
    } as ReturnType<typeof useRuntimeConfig>);
  });

  it.each(["WORKFLOW_COPILOT_CODE_BLOCK_MODE", "CODE_BLOCK_ACCESS"])(
    "exposes the OSS runtime value for %s",
    (flagName) => {
      const { result } = renderHook(() => useFeatureFlag(flagName), {
        wrapper,
      });

      expect(result.current).toBe(true);
    },
  );

  it("keeps code authoring unavailable when runtime execution access is disabled", () => {
    useRuntimeConfigMock.mockReturnValue({
      data: {
        workflow_copilot_code_block_mode: true,
        code_block_access: false,
      },
    } as ReturnType<typeof useRuntimeConfig>);

    const { result } = renderHook(
      () => ({
        codeMode: useFeatureFlag("WORKFLOW_COPILOT_CODE_BLOCK_MODE"),
        codeAccess: useFeatureFlag("CODE_BLOCK_ACCESS"),
      }),
      { wrapper },
    );

    expect(result.current).toEqual({ codeMode: true, codeAccess: false });
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

    const { result } = renderHook(
      () => useFeatureFlag("WORKFLOW_COPILOT_CODE_BLOCK_MODE"),
      { wrapper },
    );

    expect(result.current).toBeUndefined();
  });
});
