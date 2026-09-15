// @vitest-environment jsdom

import { fireEvent, render, screen } from "@testing-library/react";
import { TooltipProvider } from "@/components/ui/tooltip";
import { copyText } from "@/util/copyText";
import { type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { type StudioPaneId } from "./panes";
import { paneAccessibleName } from "./paneMeta";
import { paneExpansionKeyframes } from "./paneLayout";
import { StudioPane } from "./StudioShell";

describe("paneExpansionKeyframes", () => {
  test("grows from the pane's current bounds into its final bounds", () => {
    expect(
      paneExpansionKeyframes(
        { left: 260, top: 100, width: 300, height: 500 },
        { left: 20, top: 40, width: 1000, height: 600 },
      ),
    ).toEqual([
      {
        transform: "translate(240px, 60px) scale(0.3, 0.8333333333333334)",
        transformOrigin: "top left",
      },
      { transform: "none", transformOrigin: "top left" },
    ]);
  });
});

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
    expanded = false,
    expansionTransitioning = false,
    onToggleExpanded = vi.fn(),
    transitionFromBounds,
    onTransitionEnd,
  }: {
    id?: StudioPaneId;
    runId?: string;
    headerActions?: ReactNode;
    expanded?: boolean;
    expansionTransitioning?: boolean;
    onToggleExpanded?: () => void;
    transitionFromBounds?: {
      left: number;
      top: number;
      width: number;
      height: number;
    };
    onTransitionEnd?: () => void;
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
          expanded={expanded}
          expansionTransitioning={expansionTransitioning}
          onToggleExpanded={onToggleExpanded}
          transitionFromBounds={transitionFromBounds}
          onTransitionEnd={onTransitionEnd}
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
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    Reflect.deleteProperty(HTMLElement.prototype, "animate");
  });

  test("gives each pane a clear two-pixel outline", () => {
    const { pane } = renderPane();

    expect(pane.className).toContain("border-2");
    expect(pane.className).toContain("border-border");
  });

  test("expands a pane to the full studio stage from its header", () => {
    const onToggleExpanded = vi.fn();
    const { pane } = renderPane({ onToggleExpanded });
    const expand = screen.getByRole("button", {
      name: "Expand Copilot pane",
    });

    expect(expand.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(expand);

    expect(onToggleExpanded).toHaveBeenCalledTimes(1);
    expect(pane.className).not.toContain("absolute");
  });

  test("restores an expanded pane to the multi-pane layout", () => {
    const onToggleExpanded = vi.fn();
    const { pane } = renderPane({ expanded: true, onToggleExpanded });
    const restore = screen.getByRole("button", {
      name: "Restore Copilot pane",
    });

    expect(restore.getAttribute("aria-pressed")).toBe("true");
    expect(pane.className).toContain("absolute");
    expect(pane.className).toContain("inset-3");
    fireEvent.click(restore);

    expect(onToggleExpanded).toHaveBeenCalledTimes(1);
  });

  test("prevents another fullscreen toggle during the transition", () => {
    const onToggleExpanded = vi.fn();
    renderPane({ expansionTransitioning: true, onToggleExpanded });
    const expand = screen.getByRole("button", {
      name: "Expand Copilot pane",
    });

    expect(expand.getAttribute("aria-disabled")).toBe("true");
    expect((expand as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(expand);
    expect(onToggleExpanded).not.toHaveBeenCalled();
  });

  test("animates from the measured pane bounds over 200ms", () => {
    const animation = {
      cancel: vi.fn(),
      onfinish: null,
    } as unknown as Animation;
    const animate = vi.fn(() => animation);
    Object.defineProperty(HTMLElement.prototype, "animate", {
      configurable: true,
      value: animate,
    });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      left: 20,
      top: 40,
      width: 1000,
      height: 600,
    } as DOMRect);
    const onTransitionEnd = vi.fn();

    renderPane({
      transitionFromBounds: {
        left: 260,
        top: 100,
        width: 300,
        height: 500,
      },
      onTransitionEnd,
    });

    expect(animate).toHaveBeenCalledWith(
      paneExpansionKeyframes(
        { left: 260, top: 100, width: 300, height: 500 },
        { left: 20, top: 40, width: 1000, height: 600 },
      ),
      {
        duration: 200,
        easing: "cubic-bezier(0.22, 1, 0.36, 1)",
        fill: "both",
      },
    );
    animation.onfinish?.(new Event("finish") as AnimationPlaybackEvent);
    expect(onTransitionEnd).toHaveBeenCalledTimes(1);
  });

  test("finishes immediately when reduced motion is preferred", () => {
    const animate = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "animate", {
      configurable: true,
      value: animate,
    });
    vi.stubGlobal(
      "matchMedia",
      vi.fn(() => ({ matches: true })),
    );
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      left: 20,
      top: 40,
      width: 1000,
      height: 600,
    } as DOMRect);
    const onTransitionEnd = vi.fn();

    renderPane({
      transitionFromBounds: {
        left: 260,
        top: 100,
        width: 300,
        height: 500,
      },
      onTransitionEnd,
    });

    expect(animate).not.toHaveBeenCalled();
    expect(onTransitionEnd).toHaveBeenCalledTimes(1);
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
