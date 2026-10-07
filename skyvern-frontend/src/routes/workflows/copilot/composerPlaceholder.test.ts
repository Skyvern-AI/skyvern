import { describe, expect, it } from "vitest";

import { composerPlaceholder } from "./composerPlaceholder";

const base = {
  queuedPrompt: null,
  isLoading: false,
  isWaitingForLiveBrowser: false,
  latestTurnIsAsk: false,
};

describe("composerPlaceholder", () => {
  it("invites an answer while the latest turn is an ask", () => {
    expect(composerPlaceholder({ ...base, latestTurnIsAsk: true })).toBe(
      "Type your answer…",
    );
    expect(
      composerPlaceholder({
        ...base,
        latestTurnIsAsk: true,
        askPartChoices: "unpicked",
      }),
    ).toBe("Or type your own…");
    expect(
      composerPlaceholder({
        ...base,
        latestTurnIsAsk: true,
        askPartChoices: "picked",
      }),
    ).toBe("Add details…");
  });

  it("asks for the text a picked choice needs", () => {
    expect(
      composerPlaceholder({
        ...base,
        latestTurnIsAsk: true,
        askPartChoices: "picked",
        detailPrompt: "Which restaurant?",
      }),
    ).toBe("Which restaurant?");
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
