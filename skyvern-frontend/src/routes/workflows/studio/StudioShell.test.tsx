// @vitest-environment jsdom

import { fireEvent, render, screen } from "@testing-library/react";
import { TooltipProvider } from "@/components/ui/tooltip";
import { copyText } from "@/util/copyText";
import { type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { type StudioPaneId } from "./panes";
import { paneAccessibleName } from "./paneMeta";
import { StudioPane } from "./StudioShell";

vi.mock("@/util/copyText", () => ({ copyText: vi.fn() }));

const mockedCopyText = vi.mocked(copyText);

// Chromium aborts a native drag when the DOM mutates inside the dragstart
// task, so the reorder state (drop overlays, source dim) must engage on a
// later task. These tests pin that timing contract; only a real mouse drag
// can prove the native drag itself survives.
describe("StudioPane header", () => {
  const dataTransfer = () => ({ setData: vi.fn(), effectAllowed: "" });

  const renderPane = ({
    id = "copilot",
    runId,
    headerActions,
  }: {
    id?: StudioPaneId;
    runId?: string;
    headerActions?: ReactNode;
  } = {}) => {
    const reorder = {
      draggingId: null,
      placement: null,
      onStart: vi.fn(),
      onEnd: vi.fn(),
      onDrop: vi.fn(),
      onMove: vi.fn(),
    };
    render(
      <TooltipProvider delayDuration={0}>
        <StudioPane
          id={id}
          runId={runId}
          open
          order={0}
          flex={undefined}
          reorder={reorder}
          onClose={vi.fn()}
          headerActions={headerActions}
        >
          <div>content</div>
        </StudioPane>
      </TooltipProvider>,
    );
    return {
      reorder,
      pane: screen.getByRole("region", { name: paneAccessibleName(id) }),
      header: screen.getByRole("group", {
        name: `${paneAccessibleName(id)} pane header`,
      }),
    };
  };

  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
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

  test("a drag starting on the run id copy control is prevented", () => {
    const { reorder, header } = renderPane({
      id: "overview",
      runId: "wr_5574abcdef",
    });

    fireEvent.pointerDown(
      screen.getByRole("button", { name: "Copy to clipboard" }),
    );
    const notPrevented = fireEvent.dragStart(header, {
      dataTransfer: dataTransfer(),
    });

    expect(notPrevented).toBe(false);
    vi.runAllTimers();
    expect(reorder.onStart).not.toHaveBeenCalled();
  });

  test("shows the full run id on hover and copies it from the header control", () => {
    const runId = "wr_5574abcdef";
    renderPane({ id: "overview", runId });

    expect(screen.getByText("Run: wr_5574…")).toBeTruthy();
    const fullRunId = screen.getByText(`Run: ${runId}`);
    expect(fullRunId.getAttribute("title")).toBe(`Run: ${runId}`);
    expect(fullRunId.className).toContain("truncate");

    fireEvent.click(screen.getByRole("button", { name: "Copy to clipboard" }));

    expect(mockedCopyText).toHaveBeenCalledWith(runId);
  });

  test("groups pane utilities separately from close", () => {
    const { header } = renderPane({
      id: "browser",
      headerActions: (
        <>
          <button type="button">Reconnect</button>
          <button type="button">Open in new tab</button>
        </>
      ),
    });

    const actions = header.querySelector("[data-pane-header-actions]");
    expect(actions).not.toBeNull();
    if (!actions) {
      throw new Error("pane action cluster was not rendered");
    }
    expect(
      actions.contains(screen.getByRole("button", { name: "Reconnect" })),
    ).toBe(true);
    expect(
      actions.contains(screen.getByRole("button", { name: "Open in new tab" })),
    ).toBe(true);
    const close = screen.getByRole("button", { name: "Close Browser pane" });
    expect(actions.contains(close)).toBe(false);
  });
});
