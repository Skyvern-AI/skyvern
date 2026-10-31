import { createElement } from "react";
import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FeatureFlagContext } from "./useFeatureFlag";
import { useWorkflowDocEditorFlag } from "./useWorkflowDocEditorFlag";

function shown(
  setting: string | undefined,
  serverFlag: boolean | undefined,
): boolean {
  if (setting === undefined) {
    vi.unstubAllEnvs();
  } else {
    vi.stubEnv("VITE_WORKFLOW_DOC_EDITOR_ENABLED", setting);
  }
  return renderHook(() => useWorkflowDocEditorFlag(), {
    wrapper: ({ children }) =>
      createElement(FeatureFlagContext.Provider, {
        value: () => serverFlag,
        children,
      }),
  }).result.current;
}

describe("useWorkflowDocEditorFlag", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("hides the Doc view when the setting is off, even if opted in", () => {
    expect(shown("false", true)).toBe(false);
  });

  it("hides it when the server has not enabled the flag", () => {
    expect(shown(undefined, false)).toBe(false);
    expect(shown("true", false)).toBe(false);
    expect(shown("true", undefined)).toBe(false);
  });

  it("shows it when the setting is on (the default) and the server enables the flag", () => {
    expect(shown(undefined, true)).toBe(true);
    expect(shown("true", true)).toBe(true);
  });
});
