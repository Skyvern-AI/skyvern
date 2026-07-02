// @vitest-environment jsdom

import { beforeEach, describe, expect, test } from "vitest";

import {
  STUDIO_SHELL_STORAGE_KEY,
  useStudioShellStore,
} from "./StudioShellStore";

beforeEach(() => {
  localStorage.clear();
  useStudioShellStore.getState().reset();
});

describe("StudioShellStore", () => {
  test("defaults PiP to expanded", () => {
    expect(useStudioShellStore.getState().pipMinimized).toBe(false);
  });

  test("persists only PiP shell state", () => {
    useStudioShellStore.getState().setPipMinimized(true);

    const raw = localStorage.getItem(STUDIO_SHELL_STORAGE_KEY);
    expect(raw).not.toBeNull();
    expect(JSON.parse(raw!).state).toEqual({ pipMinimized: true });
  });

  test("migrates stale v0 Copilot collapse without restoring it", async () => {
    localStorage.setItem(
      STUDIO_SHELL_STORAGE_KEY,
      JSON.stringify({
        state: { copilotCollapsed: true, pipMinimized: true },
        version: 0,
      }),
    );

    await useStudioShellStore.persist.rehydrate();

    const state = useStudioShellStore.getState();
    expect(state.pipMinimized).toBe(true);
    expect(
      "copilotCollapsed" in (state as unknown as Record<string, unknown>),
    ).toBe(false);
  });
});
