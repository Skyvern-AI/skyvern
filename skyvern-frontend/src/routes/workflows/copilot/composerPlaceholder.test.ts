import { describe, expect, it } from "vitest";

import { composerPlaceholder } from "./composerPlaceholder";

const base = {
  queuedPrompt: false,
  isLoading: false,
  isWaitingForLiveBrowser: false,
  latestTurnIsAsk: false,
};

describe("composerPlaceholder", () => {
  it("invites an answer while the latest turn is an ask", () => {
    expect(composerPlaceholder({ ...base, latestTurnIsAsk: true })).toBe(
      "Answer Copilot…",
    );
  });

  it("returns to the standing invitation once the ask is answered", () => {
    expect(composerPlaceholder(base)).toBe(
      "Ask Copilot to build or change your workflow…",
    );
  });

  it("lets an in-flight turn outrank the ask state", () => {
    expect(
      composerPlaceholder({ ...base, latestTurnIsAsk: true, isLoading: true }),
    ).toBe("Type to queue a message…");
  });
});
