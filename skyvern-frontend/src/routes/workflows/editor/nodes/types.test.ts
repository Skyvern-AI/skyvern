import { describe, expect, it } from "vitest";

import { CODE_BLOCK_TITLE_MAX_LENGTH, getCodeBlockTitle } from "./types";

describe("getCodeBlockTitle", () => {
  it("prefers a normalized prompt over the first step title", () => {
    expect(
      getCodeBlockTitle({
        prompt: "  Collect\ninvoice details  ",
        steps: [{ title: "Run a script" }],
      }),
    ).toBe("Collect invoice details");
  });

  it("falls back to the first step title and then Code", () => {
    expect(
      getCodeBlockTitle({
        prompt: null,
        steps: [{ title: "Summarize the page" }],
      }),
    ).toBe("Summarize the page");

    expect(
      getCodeBlockTitle({
        prompt: "   ",
        steps: [{ title: null }],
      }),
    ).toBe("Code");
  });

  it("truncates long titles", () => {
    const title = getCodeBlockTitle({
      prompt: "a".repeat(CODE_BLOCK_TITLE_MAX_LENGTH + 20),
      steps: null,
    });

    expect(title).toHaveLength(CODE_BLOCK_TITLE_MAX_LENGTH);
    expect(title.endsWith("…")).toBe(true);
  });
});
