import { describe, expect, it } from "vitest";

import cases from "./workflow_note_readings.json";
import { noteReadings } from "./noteSequences";

describe("noteReadings runtime parity", () => {
  it.each(cases)("$name", ({ definition, expected }) => {
    const { textsByLabel } = noteReadings(
      definition.blocks,
      definition.finally_block_label ?? null,
    );
    expect(
      Object.fromEntries(
        [...textsByLabel].map(([label, texts]) => [label, texts.join("\n\n")]),
      ),
    ).toEqual(expected);
  });

  it("reads notes on a 10,000-block chain without overflowing the stack", () => {
    const blocks = Array.from({ length: 10_000 }, (_, index) => ({
      label: `step_${index}`,
      block_type: "navigation",
      next_block_label: index < 9_999 ? `step_${index + 1}` : null,
      notes: `Note ${index}.`,
    }));
    expect(noteReadings(blocks).textsByLabel.get("step_0")).toEqual([
      "Note 0.",
      "Note 1.",
    ]);
  });
});
