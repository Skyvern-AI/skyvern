// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { EmberWorkingStatus } from "./EmberWorkingStatus";

afterEach(() => {
  cleanup();
});

describe("EmberWorkingStatus", () => {
  it("announces each state through one live region, without the cycling verb", () => {
    const { rerender } = render(
      <EmberWorkingStatus status="working" queued={false} />,
    );
    const liveRegion = screen.getByText("Working");
    expect(liveRegion.getAttribute("aria-live")).toBe("polite");

    rerender(<EmberWorkingStatus status="working" queued />);
    expect(screen.getByText("Message queued")).toBe(liveRegion);
    // The verb re-renders every few seconds, so it must stay out of the
    // announcement or a screen reader repeats it forever.
    const visible = screen.getByTestId(
      "ember-working-status",
    ).firstElementChild;
    expect(visible?.getAttribute("aria-hidden")).toBe("true");

    rerender(<EmberWorkingStatus status="waiting" queued={false} />);
    expect(
      screen.getByText("Waiting for you", { selector: "[aria-live]" }),
    ).toBe(liveRegion);
  });
});
