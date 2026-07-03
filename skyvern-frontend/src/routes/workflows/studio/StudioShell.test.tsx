// @vitest-environment jsdom

import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { StudioPane } from "./StudioShell";

// Chromium aborts a native drag when the DOM mutates inside the dragstart
// task, so the reorder state (drop overlays, source dim) must engage on a
// later task. These tests pin that timing contract; only a real mouse drag
// can prove the native drag itself survives.
describe("StudioPane header drag", () => {
  const dataTransfer = () => ({ setData: vi.fn(), effectAllowed: "" });

  const renderPane = () => {
    const reorder = {
      draggingId: null,
      placement: null,
      onStart: vi.fn(),
      onEnd: vi.fn(),
      onDrop: vi.fn(),
      onMove: vi.fn(),
    };
    render(
      <StudioPane
        id="copilot"
        open
        order={0}
        flex={undefined}
        reorder={reorder}
        onClose={vi.fn()}
      >
        <div>content</div>
      </StudioPane>,
    );
    return {
      reorder,
      header: screen.getByRole("group", { name: "Copilot pane header" }),
    };
  };

  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  test("dragstart sets the drag payload synchronously but engages reorder on a later task", () => {
    const { reorder, header } = renderPane();
    const dt = dataTransfer();

    fireEvent.dragStart(header, { dataTransfer: dt });

    expect(dt.setData).toHaveBeenCalledWith(
      "application/x-skyvern-studio-pane",
      "copilot",
    );
    expect(reorder.onStart).not.toHaveBeenCalled();

    vi.runAllTimers();
    expect(reorder.onStart).toHaveBeenCalledTimes(1);
  });

  test("a drag cancelled before it engages never turns the reorder state on", () => {
    const { reorder, header } = renderPane();

    fireEvent.dragStart(header, { dataTransfer: dataTransfer() });
    fireEvent.dragEnd(header);

    vi.runAllTimers();
    expect(reorder.onStart).not.toHaveBeenCalled();
    expect(reorder.onEnd).toHaveBeenCalledTimes(1);
  });

  test("a drag starting on a header button is prevented", () => {
    const { reorder, header } = renderPane();

    fireEvent.pointerDown(
      screen.getByRole("button", { name: "Close Copilot pane" }),
    );
    const notPrevented = fireEvent.dragStart(header, {
      dataTransfer: dataTransfer(),
    });

    expect(notPrevented).toBe(false);
    vi.runAllTimers();
    expect(reorder.onStart).not.toHaveBeenCalled();
  });
});
