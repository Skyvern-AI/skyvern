import { describe, expect, test } from "vitest";

import type { DocItem } from "./buildDocTree";
import { demonstrationLineIndex } from "./docDemonstration";

const step = (nodeId: string): DocItem => ({ kind: "step", nodeId });
const note = (
  ownerNodeId: string,
  placement: "above" | "trailing",
): DocItem => ({ kind: "note", ownerNodeId, placement, text: "n" });
const labelOf = (id: string) => id;

describe("demonstrationLineIndex", () => {
  const items = [
    note("a", "above"),
    step("a"),
    note("a", "trailing"),
    step("b"),
  ];

  test("goes after the step it follows and that step's trailing notes", () => {
    expect(
      demonstrationLineIndex(items, labelOf, {
        after_label: "a",
        before_label: "b",
      }),
    ).toBe(3);
  });

  test("goes before the step it precedes and the notes written above it", () => {
    expect(
      demonstrationLineIndex(items, labelOf, {
        after_label: null,
        before_label: "a",
      }),
    ).toBe(0);
  });

  test("goes before a step that always runs last when the insertion names no step", () => {
    const withFinally: DocItem[] = [
      step("a"),
      note("z", "above"),
      { kind: "step", nodeId: "z", finally: true },
    ];
    expect(demonstrationLineIndex(withFinally, labelOf, null)).toBe(1);
  });

  test("draws no line when the insertion names steps this sequence does not hold", () => {
    expect(
      demonstrationLineIndex(items, labelOf, {
        after_label: "inner1",
        before_label: "inner2",
      }),
    ).toBe(-1);
  });

  test("goes at the end when the insertion names no step", () => {
    expect(demonstrationLineIndex(items, labelOf, null)).toBe(items.length);
  });
});
