// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Status } from "@/api/types";
import { useBlockPlayRegistry } from "@/store/BlockPlayRegistryStore";
import type { WorkflowRunBlock } from "../../types/workflowRunTypes";
import type { WorkflowBlockNode } from "../nodes";
import { getDocStepSummary } from "./docStepRegistry";
import { DocConditional } from "./DocConditional";
import type { DocItem } from "./buildDocTree";
import { DocRunStepButton } from "./DocRunStepButton";
import { DocStepResult } from "./DocStepResult";
import { DocScope } from "./DocScope";

const docRun = vi.hoisted(() => ({ current: {} as Record<string, unknown> }));
vi.mock("./useDocRun", () => ({ useDocRun: () => docRun.current }));

function block(overrides: Partial<WorkflowRunBlock>): WorkflowRunBlock {
  return {
    workflow_run_block_id: "wrb_1",
    workflow_run_id: "wr_1",
    parent_workflow_run_block_id: null,
    block_type: "extraction",
    label: "get_invoices",
    status: Status.Completed,
    duration: 9,
    failure_reason: null,
    actions: null,
    output: { extracted_information: [{ n: 1 }, { n: 2 }, { n: 3 }] },
    ...overrides,
  } as WorkflowRunBlock;
}

function renderResult(b: WorkflowRunBlock, live = false) {
  docRun.current = {
    runId: "wr_1",
    run: { downloaded_file_urls: [] },
    blocksByLabel: new Map([[b.label as string, b]]),
    live,
  };
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <DocStepResult label={b.label as string} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  useBlockPlayRegistry.setState({ handles: {} });
});

describe("DocStepResult", () => {
  it("states the outcome with its duration, links to the real run page and expands to the output", () => {
    renderResult(block({}));
    expect(screen.getByText("Found 3 items · 9s")).toBeTruthy();
    expect(
      screen.getByRole("link", { name: /open run/i }).getAttribute("href"),
    ).toBe("/runs/wr_1");
    expect(screen.queryByText("extracted_information")).toBeNull();
    fireEvent.click(
      screen.getByRole("button", { name: /show result of get_invoices/i }),
    );
    expect(screen.getByText("extracted_information")).toBeTruthy();
  });

  it("shows the failure reason on a failed step and renders nothing for a step with no run row", () => {
    const { container } = renderResult(
      block({
        status: Status.Failed,
        failure_reason:
          "Failed to create browser context. Executable missing at /opt/x/chrome\n+----+\n| Looks like it was just installed |\n+----+",
      }),
    );
    expect(
      screen.getByText(/^Failed: Failed to create browser context/),
    ).toBeTruthy();
    expect(screen.queryByText(/opt\/x\/chrome/)).toBeNull();
    docRun.current = {
      runId: "wr_1",
      run: undefined,
      blocksByLabel: new Map(),
      live: false,
    };
    const empty = render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <DocStepResult label="other" />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(
      empty.container.querySelector("[data-testid=doc-step-result]"),
    ).toBeNull();
    expect(
      container.querySelector("[data-testid=doc-step-result]"),
    ).toBeTruthy();
  });
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

describe("DocConditional on a run", () => {
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
  const renderConditional = (executed: string | null | undefined) => {
    docRun.current = {
      runId: executed === undefined ? null : "wr_1",
      blocksByLabel:
        executed === undefined
          ? new Map()
          : new Map([
              [
                "check_box",
                block({
                  label: "check_box",
                  block_type: "conditional",
                  executed_branch_id: executed,
                }),
              ],
            ]),
      live: false,
    };
    return render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <DocConditional
            node={conditional}
            item={item}
            byId={new Map()}
            editable={false}
            renderChildren={() => null}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  };
  const tab = (name: RegExp) => screen.getByRole("tab", { name });

  it("opens and marks the branch the run took", () => {
    renderConditional("b_else");
    expect(tab(/otherwise/i).getAttribute("aria-selected")).toBe("true");
    expect(tab(/otherwise/i).textContent).toMatch(/ran/);
    expect(tab(/^if/i).textContent).not.toMatch(/ran/);
  });

  it("finds the branch by the steps that ran when the run predates a save", () => {
    const tick = {
      id: "t1",
      type: "navigation",
      data: { label: "tick_on" },
    } as unknown as WorkflowBlockNode;
    docRun.current = {
      runId: "wr_1",
      blocksByLabel: new Map([
        [
          "check_box",
          block({
            label: "check_box",
            block_type: "conditional",
            executed_branch_id: "old_id",
          }),
        ],
        ["tick_on", block({ label: "tick_on" })],
      ]),
      live: false,
    };
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <DocConditional
            node={conditional}
            item={
              {
                ...item,
                branches: [
                  item.branches[0]!,
                  {
                    ...item.branches[1]!,
                    children: [{ kind: "step", nodeId: "t1" }],
                  },
                ],
              } as typeof item
            }
            byId={new Map([["t1", tick]])}
            editable={false}
            renderChildren={() => null}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(tab(/otherwise/i).getAttribute("aria-selected")).toBe("true");
  });

  it("does not guess a branch for a run that recorded none", () => {
    renderConditional(null);
    expect(tab(/^if/i).getAttribute("aria-selected")).toBe("true");
    expect(screen.queryByText("ran")).toBeNull();
  });

  it("opens the first branch when there is no run", () => {
    renderConditional(undefined);
    expect(tab(/^if/i).getAttribute("aria-selected")).toBe("true");
    expect(screen.queryByText("ran")).toBeNull();
  });
});

it("shows the run result of an executed step in a read-only region", () => {
  docRun.current = {
    runId: "wr_1",
    run: { downloaded_file_urls: [] },
    blocksByLabel: new Map([["get_invoices", block({})]]),
    live: false,
  };
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <DocScope
          items={[
            {
              kind: "unplaced",
              nodeIds: ["n1"],
              readOnlyReason: "Edit these steps in Visual",
            } as DocItem,
          ]}
          byId={new Map([["n1", stepNode]])}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  expect(screen.getByText("Found 3 items · 9s")).toBeTruthy();
});
