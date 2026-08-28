// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { StreamStatusPanel } from "./StreamDiagnostics";

afterEach(cleanup);

describe("StreamStatusPanel", () => {
  it("announces the pending stream state without exposing the rotating whimsy", () => {
    render(
      <StreamStatusPanel
        diagnostic={{
          title: "Waking up your local browser",
          detail: "Opening the stream and waiting for the first frame...",
          pending: true,
        }}
      />,
    );

    const status = screen.getByRole("status");
    expect(status.textContent).toContain("Waking up your local browser");
    expect(status.textContent).toContain("waiting for the first frame");

    // The whimsical messages cycle on a timer; left readable, the panel would
    // re-interrupt a screen reader on every rotation.
    const whimsy = screen.getByText(/still working on it/i);
    expect(whimsy.closest('[aria-hidden="true"]')).toBeTruthy();
  });

  it("announces a terminal diagnostic with the loading animation gone", () => {
    render(
      <StreamStatusPanel
        diagnostic={{
          title: "This browser session has expired",
          detail: "It reached its timeout and was shut down.",
        }}
      />,
    );

    const status = screen.getByRole("status");
    expect(status.textContent).toContain("This browser session has expired");
    expect(screen.queryByText(/still working on it/i)).toBeNull();
  });
});
