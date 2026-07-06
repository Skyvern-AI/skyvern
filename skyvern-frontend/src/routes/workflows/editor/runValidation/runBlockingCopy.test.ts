import { describe, expect, it } from "vitest";

import { getRunBlockingTooltipText } from "./runBlockingCopy";

describe("getRunBlockingTooltipText", () => {
  it("describes the empty fallback contract", () => {
    expect(getRunBlockingTooltipText([])).toBe(
      "Select credentials for login blocks before running.",
    );
  });

  it("describes one blocking login block", () => {
    expect(getRunBlockingTooltipText([{ id: "n1", label: "block_2" }])).toBe(
      'Select a credential for the login block "block_2" before running.',
    );
  });

  it("lists multiple blocking login blocks", () => {
    expect(
      getRunBlockingTooltipText([
        { id: "n1", label: "block_2" },
        { id: "n3", label: "block_3" },
      ]),
    ).toBe(
      "Select credentials for these login blocks before running: block_2, block_3.",
    );
  });
});
