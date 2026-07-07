// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { getNodesBounds } from "@xyflow/react";
import type { CSSProperties, ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";

import type { AppNode } from "../nodes";
import { endAnchoredViewport, startAnchoredViewport } from "../paneFit";

import { FlowJumpControls } from "./FlowJumpControls";

// The studio shell provides the Radix tooltip context in production.
function renderControls(nodes: Array<AppNode>) {
  return render(
    <TooltipProvider delayDuration={0}>
      <FlowJumpControls nodes={nodes} />
    </TooltipProvider>,
  );
}

const mocks = vi.hoisted(() => ({
  setViewport: vi.fn(),
  getNodes: vi.fn((): Array<unknown> => []),
  storeState: {
    width: 800,
    height: 600,
    transform: [150, 24, 1] as [number, number, number],
    domNode: null as HTMLElement | null,
  },
}));

vi.mock("@xyflow/react", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@xyflow/react")>();
  return {
    ...actual,
    Panel: ({
      children,
      position,
      style,
    }: {
      children?: ReactNode;
      position?: string;
      style?: CSSProperties;
    }) => (
      <div data-testid={`panel-${position}`} style={style}>
        {children}
      </div>
    ),
    useReactFlow: () => ({
      setViewport: mocks.setViewport,
      getNodes: mocks.getNodes,
    }),
    useStore: (selector: (state: unknown) => unknown) =>
      selector(mocks.storeState),
  };
});

function makeNode(
  id: string,
  y: number,
  height: number,
  hidden = false,
): AppNode {
  return {
    id,
    type: "task",
    position: { x: 0, y },
    measured: { width: 480, height },
    hidden,
    data: {},
  } as unknown as AppNode;
}

// 4000 world px tall: far taller than the 600px pane at any anchor zoom.
const longFlow = [makeNode("head", 0, 100), makeNode("tail", 3900, 100)];
const shortFlow = [makeNode("head", 0, 400)];
const pane = { width: 800, height: 600 };

function stubMatchMedia(reducedMotion: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({ matches: reducedMotion })),
  );
}

// jsdom pane containing a fake Controls stack, with layout stubbed: the pane
// bottom sits at y=600 and the stack's top at y=420.
function stubPaneWithControls() {
  const pane = document.createElement("div");
  pane.getBoundingClientRect = () =>
    ({ top: 0, bottom: 600, left: 0, right: 800 }) as DOMRect;
  const controls = document.createElement("div");
  controls.className = "react-flow__controls";
  controls.getBoundingClientRect = () =>
    ({ top: 420, bottom: 585, left: 15, right: 41 }) as DOMRect;
  pane.appendChild(controls);
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  return pane;
}

describe("FlowJumpControls", () => {
  beforeEach(() => {
    mocks.storeState.width = pane.width;
    mocks.storeState.height = pane.height;
    mocks.storeState.transform = [150, 24, 1];
    mocks.storeState.domNode = null;
    mocks.getNodes.mockReturnValue(longFlow);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    mocks.setViewport.mockReset();
    mocks.getNodes.mockReset();
  });

  test("a flow that fits the pane shows neither button", () => {
    renderControls(shortFlow);
    expect(screen.queryByRole("button", { name: "Jump to start" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Jump to end" })).toBeNull();
  });

  test("hidden nodes do not extend the flow into jumpable territory", () => {
    renderControls([...shortFlow, makeNode("collapsed", 5000, 100, true)]);
    expect(screen.queryByRole("button", { name: "Jump to start" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Jump to end" })).toBeNull();
  });

  test("at the flow's start only the jump to its end shows", () => {
    renderControls(longFlow);
    expect(screen.queryByRole("button", { name: "Jump to start" })).toBeNull();
    expect(screen.getByRole("button", { name: "Jump to end" })).toBeInstanceOf(
      HTMLButtonElement,
    );
  });

  test("at the flow's end only the jump back to its start shows", () => {
    mocks.storeState.transform = [150, 600 - 24 - 4000, 1];
    renderControls(longFlow);
    expect(
      screen.getByRole("button", { name: "Jump to start" }),
    ).toBeInstanceOf(HTMLButtonElement);
    expect(screen.queryByRole("button", { name: "Jump to end" })).toBeNull();
  });

  test("scrolled mid-flow shows both jumps", () => {
    mocks.storeState.transform = [150, -1700, 1];
    renderControls(longFlow);
    expect(
      screen.getByRole("button", { name: "Jump to start" }),
    ).toBeInstanceOf(HTMLButtonElement);
    expect(screen.getByRole("button", { name: "Jump to end" })).toBeInstanceOf(
      HTMLButtonElement,
    );
  });

  test("both buttons mount on the left rail (top-left / bottom-left panels)", () => {
    mocks.storeState.transform = [150, -1700, 1];
    renderControls(longFlow);
    expect(
      screen
        .getByTestId("panel-top-left")
        .contains(screen.getByRole("button", { name: "Jump to start" })),
    ).toBe(true);
    expect(
      screen
        .getByTestId("panel-bottom-left")
        .contains(screen.getByRole("button", { name: "Jump to end" })),
    ).toBe(true);
  });

  test("the bottom panel clears the Controls stack by the measured height plus gap", () => {
    mocks.storeState.domNode = stubPaneWithControls();
    mocks.storeState.transform = [150, -1700, 1];
    renderControls(longFlow);
    // Pane bottom 600 - controls top 420 + 8px gap.
    expect(screen.getByTestId("panel-bottom-left").style.marginBottom).toBe(
      "188px",
    );
  });

  test("without a Controls stack the bottom panel keeps its default inset", () => {
    mocks.storeState.transform = [150, -1700, 1];
    renderControls(longFlow);
    expect(screen.getByTestId("panel-bottom-left").style.marginBottom).toBe("");
  });

  test("jump to end pans to the end anchor of the visible nodes", () => {
    stubMatchMedia(false);
    const hiddenOutlier = makeNode("collapsed", 9000, 100, true);
    mocks.getNodes.mockReturnValue([...longFlow, hiddenOutlier]);
    renderControls([...longFlow, hiddenOutlier]);

    fireEvent.click(screen.getByRole("button", { name: "Jump to end" }));

    expect(mocks.setViewport).toHaveBeenCalledTimes(1);
    expect(mocks.setViewport).toHaveBeenCalledWith(
      endAnchoredViewport({ pane, bounds: getNodesBounds(longFlow) }),
      { duration: 150 },
    );
  });

  test("jump to start pans to the start anchor, instant under reduced motion", () => {
    stubMatchMedia(true);
    mocks.storeState.transform = [150, 600 - 24 - 4000, 1];
    renderControls(longFlow);

    fireEvent.click(screen.getByRole("button", { name: "Jump to start" }));

    expect(mocks.setViewport).toHaveBeenCalledTimes(1);
    expect(mocks.setViewport).toHaveBeenCalledWith(
      startAnchoredViewport({ pane, bounds: getNodesBounds(longFlow) }),
      { duration: 0 },
    );
  });
});
