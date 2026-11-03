import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PaneErrorBoundary } from "./PaneErrorBoundary";

describe("PaneErrorBoundary", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("reports a crash, renders a fallback, and reloads the panel", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const error = new Error("panel crashed");
    let shouldThrow = true;
    const onError = vi.fn(() => {
      shouldThrow = false;
    });

    function Child() {
      if (shouldThrow) {
        throw error;
      }
      return <div>Panel recovered</div>;
    }

    render(
      <PaneErrorBoundary pane="editor" onError={onError}>
        <Child />
      </PaneErrorBoundary>,
    );

    expect(screen.getByText("This panel hit an error.")).toBeTruthy();
    expect(onError).toHaveBeenCalledOnce();
    expect(onError).toHaveBeenCalledWith(error, expect.any(String));

    fireEvent.click(screen.getByRole("button", { name: "Reload panel" }));

    expect(screen.getByText("Panel recovered")).toBeTruthy();
    expect(onError).toHaveBeenCalledOnce();
  });

  it("renders the fallback in the fallback container", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const fallbackContainer = document.createElement("div");
    document.body.appendChild(fallbackContainer);

    function Child(): null {
      throw new Error("panel crashed");
    }

    const view = render(
      <PaneErrorBoundary
        pane="copilot"
        fallbackContainer={fallbackContainer}
        onError={() => undefined}
      >
        <Child />
      </PaneErrorBoundary>,
    );

    expect(
      within(fallbackContainer).getByText("This panel hit an error."),
    ).toBeTruthy();
    expect(view.container.childElementCount).toBe(0);

    view.unmount();
    fallbackContainer.remove();
  });
});
