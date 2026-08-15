// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { NarrativeView } from "./NarrativeView";
import { EMPTY_NARRATIVE, TurnNarrativeState } from "./narrativeState";

const exploringTurn = (): TurnNarrativeState => ({
  ...EMPTY_NARRATIVE,
  turnId: "turn-1",
  turnIndex: 0,
});

afterEach(() => {
  cleanup();
});

describe("NarrativeView — transcript working line", () => {
  it("keeps the line when the composer's verb row is not showing", () => {
    render(<NarrativeView turn={exploringTurn()} uxV1 />);
    expect(screen.getByText("Working…")).toBeTruthy();
  });

  it("drops the line once the composer's verb row carries the state", () => {
    render(<NarrativeView turn={exploringTurn()} uxV1 workingRowActive />);
    expect(screen.queryByText("Working…")).toBeNull();
  });
});
