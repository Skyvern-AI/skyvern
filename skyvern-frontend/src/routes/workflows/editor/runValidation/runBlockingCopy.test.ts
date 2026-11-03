import { describe, expect, it } from "vitest";

import type { RunBlockingBlock } from "./getRunBlockingBlocks";
import { getRunBlockingTooltipText } from "./runBlockingCopy";

function block(id: string, label: string): RunBlockingBlock {
  return { id, label, ancestorIds: [], path: [] };
}

describe("getRunBlockingTooltipText", () => {
  it("describes the empty fallback contract", () => {
    expect(getRunBlockingTooltipText([])).toBe(
      "Select credentials for login blocks before running.",
    );
  });

  it("describes one blocking login block", () => {
    expect(getRunBlockingTooltipText([block("n1", "block_2")])).toBe(
      'Select a credential for the login block "block_2" before running.',
    );
  });

  it("lists multiple blocking login blocks", () => {
    expect(
      getRunBlockingTooltipText([
        block("n1", "block_2"),
        block("n3", "block_3"),
      ]),
    ).toBe(
      "Select credentials for these login blocks before running: block_2, block_3.",
    );
  });
});
