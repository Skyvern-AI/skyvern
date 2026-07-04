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

  test("persists only PiP and pane-width shell state", () => {
    useStudioShellStore.getState().setPipMinimized(true);

    const raw = localStorage.getItem(STUDIO_SHELL_STORAGE_KEY);
    expect(raw).not.toBeNull();
    expect(JSON.parse(raw!).state).toEqual({
      pipMinimized: true,
      paneWidths: {},
    });
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

  test("merges and persists pane widths; reset clears them", () => {
    useStudioShellStore.getState().setPaneWidths({ copilot: 412 });
    useStudioShellStore.getState().setPaneWidths({ editor: 350.4 });

    expect(useStudioShellStore.getState().paneWidths).toEqual({
      copilot: 412,
      editor: 350,
    });
    const raw = localStorage.getItem(STUDIO_SHELL_STORAGE_KEY);
    expect(JSON.parse(raw!).state.paneWidths).toEqual({
      copilot: 412,
      editor: 350,
    });

    useStudioShellStore.getState().resetPaneWidths();
    expect(useStudioShellStore.getState().paneWidths).toEqual({});
  });

  test("drops non-numeric persisted pane widths on rehydrate", async () => {
    localStorage.setItem(
      STUDIO_SHELL_STORAGE_KEY,
      JSON.stringify({
        state: {
          pipMinimized: false,
          paneWidths: { copilot: 320, editor: "wide", browser: -10 },
        },
        version: 0,
      }),
    );

    await useStudioShellStore.persist.rehydrate();

    expect(useStudioShellStore.getState().paneWidths).toEqual({
      copilot: 320,
    });
  });
});
