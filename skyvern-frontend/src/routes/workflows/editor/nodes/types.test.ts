import { describe, expect, it } from "vitest";

import { CODE_BLOCK_TITLE_MAX_LENGTH, getCodeBlockTitle } from "./types";

describe("getCodeBlockTitle", () => {
  it("uses the normalized prompt", () => {
    expect(
      getCodeBlockTitle({
        prompt: "  Collect\ninvoice details  ",
      }),
    ).toBe("Collect invoice details");
  });

  it("falls back to Code without a prompt", () => {
    expect(getCodeBlockTitle({ prompt: null })).toBe("Code");
    expect(getCodeBlockTitle({ prompt: "   " })).toBe("Code");
  });

  it("truncates long titles", () => {
    const title = getCodeBlockTitle({
      prompt: "a".repeat(CODE_BLOCK_TITLE_MAX_LENGTH + 20),
    });

    expect(title).toHaveLength(CODE_BLOCK_TITLE_MAX_LENGTH);
    expect(title.endsWith("…")).toBe(true);
  });
});
