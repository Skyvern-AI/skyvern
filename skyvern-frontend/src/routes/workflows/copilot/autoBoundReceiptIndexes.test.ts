import { describe, expect, it } from "vitest";

import { selectAutoBoundReceiptIndexes } from "./autoBoundReceiptIndexes";

describe("selectAutoBoundReceiptIndexes", () => {
  it("selects nothing from empty or all-null input", () => {
    expect([...selectAutoBoundReceiptIndexes([])]).toEqual([]);
    expect([...selectAutoBoundReceiptIndexes([null, null])]).toEqual([]);
  });

  it("keeps the first of consecutive repeated credentials", () => {
    expect([
      ...selectAutoBoundReceiptIndexes(["credential-a", "credential-a"]),
    ]).toEqual([0]);
  });

  it("does not let null reset the previous credential", () => {
    expect([
      ...selectAutoBoundReceiptIndexes(["credential-a", null, "credential-a"]),
    ]).toEqual([0]);
  });

  it("lets the first eligible credential claim its own index", () => {
    expect([
      ...selectAutoBoundReceiptIndexes([null, "credential-a", "credential-a"]),
    ]).toEqual([1]);
  });

  it("keeps every transition in an A to B to A sequence", () => {
    expect([
      ...selectAutoBoundReceiptIndexes([
        "credential-a",
        "credential-b",
        "credential-a",
      ]),
    ]).toEqual([0, 1, 2]);
  });

  it("preserves ascending source indexes through a long mixed sequence", () => {
    const credentialIds = Object.freeze([
      null,
      "credential-a",
      "credential-a",
      null,
      "credential-b",
      "credential-b",
      "credential-a",
      null,
      "credential-a",
      "credential-c",
    ]);

    expect([...selectAutoBoundReceiptIndexes(credentialIds)]).toEqual([
      1, 4, 6, 9,
    ]);
  });
});
