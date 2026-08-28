// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { NarrativeView } from "./NarrativeView";
import { EMPTY_NARRATIVE, TurnNarrativeState } from "./narrativeState";

const exploringTurn = (): TurnNarrativeState => ({
  ...EMPTY_NARRATIVE,
  turnId: "turn-1",
  turnIndex: 0,
  designStarted: true,
});

afterEach(() => {
  cleanup();
});

describe("NarrativeView — transcript working line", () => {
  it("replaces the active-turn working header with the acknowledgement", () => {
    render(<NarrativeView turn={exploringTurn()} />);

    expect(screen.queryByText("Working…")).toBeNull();
    expect(screen.queryByText("· building your workflow")).toBeNull();
    expect(screen.getByRole("status").textContent).toContain(
      "Copilot is working on your request…",
    );
  });
});
