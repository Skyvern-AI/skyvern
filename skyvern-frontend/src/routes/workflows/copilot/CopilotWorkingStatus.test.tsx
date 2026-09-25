// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { CopilotWorkingStatus } from "./CopilotWorkingStatus";

afterEach(() => {
  cleanup();
});

describe("CopilotWorkingStatus", () => {
  it("announces the state once, without the cycling verb", () => {
    const { rerender } = render(<CopilotWorkingStatus queued={false} />);
    expect(screen.getByText("Working")).toBeTruthy();

    rerender(<CopilotWorkingStatus queued />);
    expect(screen.getByText("Message queued")).toBeTruthy();
    // The verb re-renders every few seconds, so it must stay out of the
    // announcement or a screen reader repeats it forever.
    const verb = screen.getByTestId("copilot-working-status").firstElementChild;
    expect(verb?.getAttribute("aria-hidden")).toBe("true");
  });
});
