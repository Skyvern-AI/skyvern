// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useBlockPlayRegistry } from "@/store/BlockPlayRegistryStore";
import type { WorkflowBlockNode } from "../nodes";
import { getDocStepSummary } from "./docStepRegistry";
import { DocConditional } from "./DocConditional";
import { WorkflowDocActionsContext } from "./WorkflowDocActionsContext";
import type { DocItem } from "./buildDocTree";
import { DocRunStepButton } from "./DocRunStepButton";

afterEach(() => {
  useBlockPlayRegistry.setState({ handles: {} });
});

vi.mock("../nodes/components/NodeHeader", () => ({
  NodeHeader: ({ nodeId }: { nodeId: string }) => (
    <span data-testid={`host-${nodeId}`} />
  ),
}));

const stepNode = {
  id: "n1",
  type: "navigation",
  data: { label: "get_invoices" },
} as unknown as WorkflowBlockNode;

const handle = (source: "canvas" | "doc", inert = false) => ({
  play: vi.fn(),
  stop: vi.fn(),
  inert,
  playing: false,
  stopping: false,
  source,
});

describe("DocRunStepButton", () => {
  afterEach(() => useBlockPlayRegistry.setState({ handles: {} }));

  it("starts the canvas block's own run flow, and is disabled while the run is inert", () => {
    const canvas = handle("canvas");
    useBlockPlayRegistry.getState().register("n1", canvas);
    const { rerender } = render(<DocRunStepButton node={stepNode} />);
    fireEvent.click(screen.getByRole("button", { name: "Run get_invoices" }));
    expect(canvas.play).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("host-n1")).toBeNull();
    useBlockPlayRegistry.getState().register("n1", handle("canvas", true));
    rerender(<DocRunStepButton node={stepNode} />);
    expect(
      (
        screen.getByRole("button", {
          name: "Run get_invoices",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
  });

  it("disables Stop while a stop request is in flight", () => {
    useBlockPlayRegistry
      .getState()
      .register("n1", { ...handle("canvas"), playing: true, stopping: true });
    render(<DocRunStepButton node={stepNode} />);
    expect(
      (
        screen.getByRole("button", {
          name: "Stop get_invoices",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
  });

  it("hosts the run itself for a step the canvas does not render", () => {
    render(<DocRunStepButton node={stepNode} />);
    expect(screen.getByTestId("host-n1")).toBeTruthy();
  });

  it("a Doc host leaving never removes the canvas header's handle", () => {
    const canvas = handle("canvas");
    useBlockPlayRegistry.getState().register("n1", canvas);
    useBlockPlayRegistry.getState().unregister("n1", "doc");
    expect(useBlockPlayRegistry.getState().handles.n1).toBe(canvas);
  });
});

describe("getDocStepSummary wait", () => {
  it("uses the singular only for one second", () => {
    const wait = (n: number) =>
      getDocStepSummary({
        id: "w",
        type: "wait",
        position: { x: 0, y: 0 },
        data: { waitInSeconds: n },
      } as never);
    expect(wait(1)).toBe("Wait 1 second");
    expect(wait(10)).toBe("Wait 10 seconds");
  });
});

describe("DocConditional branches", () => {
  const conditional = {
    id: "c1",
    type: "conditional",
    data: { label: "check_box" },
  } as unknown as WorkflowBlockNode;
  const branch = (branchId: string, kind: "if" | "otherwise") => ({
    branchId,
    kind,
    synthetic: false,
    condition: kind === "if" ? "{{ checked }}" : "",
    check: null,
    children: [],
    stepCount: 0,
  });
  const item = {
    kind: "conditional",
    nodeId: "c1",
    branches: [branch("b_if", "if"), branch("b_else", "otherwise")],
  } as unknown as Extract<DocItem, { kind: "conditional" }>;
  const renderConditional = (openBranch?: string) =>
    render(
      <WorkflowDocActionsContext.Consumer>
        {(actions) => (
          <WorkflowDocActionsContext.Provider
            value={{ ...actions, selectedBranch: () => openBranch }}
          >
            <DocConditional
              node={conditional}
              item={item}
              byId={new Map()}
              editable={false}
              renderChildren={() => null}
            />
          </WorkflowDocActionsContext.Provider>
        )}
      </WorkflowDocActionsContext.Consumer>,
    );
  const tab = (name: RegExp) => screen.getByRole("tab", { name });

  it("opens the branch the user picked, else the first", () => {
    renderConditional();
    expect(tab(/^if/i).getAttribute("aria-selected")).toBe("true");
    cleanup();
    renderConditional("b_else");
    expect(tab(/otherwise/i).getAttribute("aria-selected")).toBe("true");
  });
});
