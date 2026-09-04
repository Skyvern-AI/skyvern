import { describe, expect, it } from "vitest";

import {
  emissionPrompts,
  questionAnswerLabel,
  questionAnswerLine,
  twinPrompt,
} from "./questionAnswerLine";

// Pins SKY-15619 mutation O: the separator was changed from " — " to ": " and the whole copilot
// suite stayed green, because every other call site computed its expectation by calling this
// function. The expected values here are LITERALS for that reason — a card rendered today reads
// the answered state out of a message written weeks ago, so this is a persisted format contract
// and a test that moves with the code cannot hold it still.
describe("questionAnswerLine", () => {
  it("emits the exact persisted wire format", () => {
    expect(questionAnswerLine("Which day?", "Friday")).toBe(
      "Which day? — Friday",
    );
    expect(questionAnswerLabel("Which day?")).toBe("Which day? — ");
  });

  it("emits the exact persisted twin format", () => {
    expect(twinPrompt("Which day?", 2)).toBe("Which day? (2)");
    expect(questionAnswerLine(twinPrompt("Which day?", 2), "Friday")).toBe(
      "Which day? (2) — Friday",
    );
  });
});

describe("emissionPrompts", () => {
  it("leaves a unique prompt plain, so no existing chat changes shape", () => {
    expect(emissionPrompts(["Which day?", "Which store?"])).toEqual([
      "Which day?",
      "Which store?",
    ]);
  });

  it("keeps the first occurrence plain and numbers the repeats", () => {
    expect(
      emissionPrompts(["Which day?", "Which store?", "Which day?"]),
    ).toEqual(["Which day?", "Which store?", "Which day? (2)"]);
  });

  // The emitted line is whitespace-normalized, and that is lossy. These two prompts are different
  // raw strings that would persist the SAME prefix, so uniqueness has to be enforced in normalized
  // space or answering the second reloads onto the first.
  it("separates prompts that differ only by whitespace", () => {
    const labels = emissionPrompts(["Which\nstore?", "Which store?"]);
    const normalized = labels.map((l) => l.replace(/\s+/g, " ").trim());

    expect(new Set(normalized).size).toBe(2);
    expect(normalized[1]).toBe("Which store? (2)");
  });
});
