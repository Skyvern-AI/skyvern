import { describe, expect, it } from "vitest";

import {
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
