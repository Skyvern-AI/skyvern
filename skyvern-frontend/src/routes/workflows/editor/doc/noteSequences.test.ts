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
});
