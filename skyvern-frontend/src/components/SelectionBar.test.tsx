import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SelectionBar } from "./SelectionBar";

describe("SelectionBar", () => {
  afterEach(cleanup);

  it("shows the count and clears on Escape", () => {
    const onClear = vi.fn();
    render(
      <SelectionBar count={3} isOperating={false} onClear={onClear}>
        <button>Action</button>
      </SelectionBar>,
    );
    expect(screen.getByText("3 selected")).toBeTruthy();
    document.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
    );
    expect(onClear).toHaveBeenCalledTimes(1);
  });

  it("ignores Escape that a Radix layer already handled", () => {
    const onClear = vi.fn();
    render(<SelectionBar count={1} isOperating={false} onClear={onClear} />);
    const event = new KeyboardEvent("keydown", {
      key: "Escape",
      bubbles: true,
      cancelable: true,
    });
    event.preventDefault();
    document.dispatchEvent(event);
    expect(onClear).not.toHaveBeenCalled();
  });

  it("shows Processing and ignores Escape while operating", () => {
    const onClear = vi.fn();
    render(<SelectionBar count={2} isOperating={true} onClear={onClear} />);
    expect(screen.getByText("Processing…")).toBeTruthy();
    document.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
    );
    expect(onClear).not.toHaveBeenCalled();
    expect(
      (screen.getByLabelText("Clear selection") as HTMLButtonElement).disabled,
    ).toBe(true);
  });
});
