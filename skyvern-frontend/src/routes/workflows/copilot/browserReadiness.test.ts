import { describe, expect, it } from "vitest";

import {
  resolveCopilotLiveBrowserReady,
  shouldQueuePromptForLiveBrowser,
  shouldWaitForLiveBrowser,
} from "./browserReadiness";

describe("shouldWaitForLiveBrowser", () => {
  it("waits when a live browser is required and not ready", () => {
    expect(
      shouldWaitForLiveBrowser({
        requiresLiveBrowser: true,
        isLiveBrowserReady: false,
      }),
    ).toBe(true);
  });

  it("does not wait once the required browser is ready", () => {
    expect(
      shouldWaitForLiveBrowser({
        requiresLiveBrowser: true,
        isLiveBrowserReady: true,
      }),
    ).toBe(false);
  });

  it("does not wait when no live browser is required", () => {
    expect(
      shouldWaitForLiveBrowser({
        requiresLiveBrowser: false,
        isLiveBrowserReady: false,
      }),
    ).toBe(false);
  });
});

describe("shouldQueuePromptForLiveBrowser", () => {
  it("queues non-empty prompts while a required live browser is not ready", () => {
    expect(
      shouldQueuePromptForLiveBrowser({
        requiresLiveBrowser: true,
        isLiveBrowserReady: false,
        message: "Build a workflow",
      }),
    ).toBe(true);
  });

  it("does not queue blank prompts", () => {
    expect(
      shouldQueuePromptForLiveBrowser({
        requiresLiveBrowser: true,
        isLiveBrowserReady: false,
        message: "   ",
      }),
    ).toBe(false);
  });

  it("does not queue once the live browser is ready", () => {
    expect(
      shouldQueuePromptForLiveBrowser({
        requiresLiveBrowser: true,
        isLiveBrowserReady: true,
        message: "Build a workflow",
      }),
    ).toBe(false);
  });
});

describe("resolveCopilotLiveBrowserReady", () => {
  describe("with the headless drain affordance off", () => {
    it.each([
      [false, false],
      [false, true],
      [true, false],
      [true, true],
    ])(
      "returns displayReady unchanged (display=%s, backend=%s)",
      (displayReady, hasBackendSession) => {
        expect(
          resolveCopilotLiveBrowserReady({
            displayReady,
            hasBackendSession,
            headlessTurnDrainEnabled: false,
          }),
        ).toBe(displayReady);
      },
    );
  });

  describe("with the headless drain affordance on", () => {
    it("becomes ready on a backend session even without a painted display", () => {
      expect(
        resolveCopilotLiveBrowserReady({
          displayReady: false,
          hasBackendSession: true,
          headlessTurnDrainEnabled: true,
        }),
      ).toBe(true);
    });

    it("stays not-ready without a backend session id", () => {
      expect(
        resolveCopilotLiveBrowserReady({
          displayReady: false,
          hasBackendSession: false,
          headlessTurnDrainEnabled: true,
        }),
      ).toBe(false);
    });

    it("stays ready when the display is ready regardless of the backend session", () => {
      expect(
        resolveCopilotLiveBrowserReady({
          displayReady: true,
          hasBackendSession: false,
          headlessTurnDrainEnabled: true,
        }),
      ).toBe(true);
    });
  });
});
