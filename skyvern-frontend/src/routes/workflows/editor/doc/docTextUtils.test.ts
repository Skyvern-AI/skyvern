import { describe, expect, it } from "vitest";

import { splitJinjaReferences, stripJinjaReferences } from "./docTextUtils";

describe("Doc Jinja reference scanning", () => {
  it("splits complete references and preserves an unclosed suffix", () => {
    const unclosed = `{{${" ".repeat(100_000)}value`;
    const value = `Read {{ user_name }} and {{  total  }} before ${unclosed}`;

    const parts = splitJinjaReferences(value);
    expect(parts.slice(0, 4)).toEqual([
      "Read ",
      { ref: "user_name" },
      " and ",
      { ref: "total" },
    ]);
    expect(parts[4]).toBe(` before ${unclosed}`);
    expect(stripJinjaReferences(value)).toBe(
      `Read user_name and total before ${unclosed}`,
    );
  });
});
