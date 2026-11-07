// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ArtifactType, Status } from "@/api/types";
import { useBlockPlayRegistry } from "@/store/BlockPlayRegistryStore";
import type { WorkflowRunBlock } from "../../types/workflowRunTypes";
import type { WorkflowBlockNode } from "../nodes";
import { getDocStepSummary } from "./docStepRegistry";
import { DocConditional } from "./DocConditional";
import { WorkflowDocActionsContext } from "./WorkflowDocActionsContext";
import type { DocItem } from "./buildDocTree";
import { DocRunStepButton } from "./DocRunStepButton";
import { DocStepResult } from "./DocStepResult";
import { DocScope } from "./DocScope";

const docRun = vi.hoisted(() => ({ current: {} as Record<string, unknown> }));
vi.mock("./useDocRun", () => ({
  useDocRun: () => ({
    rowsLoaded: true,
    loopsByLabel: new Map(),
    blockRun: false,
    unknownLabels: new Set<string>(),
    ...docRun.current,
  }),
}));

const screenshotMock = vi.hoisted(() => ({
  artifact: undefined as unknown,
}));
// Like the real hook, a disabled fetch still returns what the Run pane already cached.
vi.mock("../../workflowRun/useBlockScreenshot", () => ({
  useBlockScreenshot: () => screenshotMock.artifact,
}));
vi.mock("@/hooks/useArtifactImageSrc", () => ({
  useArtifactImageSrc: () => ({
    src: "data:image/gif;base64,R0lGODlhAQABAAAAACw=",
    onImageError: () => {},
    imageFailed: false,
  }),
}));

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
  screenshotMock.artifact = undefined;
  useBlockPlayRegistry.setState({ handles: {} });
});

describe("DocStepResult", () => {
  it("says a step of a live run is waiting, and draws no screenshot box before a capture exists", () => {
    docRun.current = {
      runId: "wr_1",
      run: { downloaded_file_urls: [] },
      blocksByLabel: new Map([
        [
          "running_step",
          block({ label: "running_step", status: Status.Running }),
        ],
      ]),
      live: true,
    };
    const ui = (label: string) => (
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <DocStepResult label={label} />
        </MemoryRouter>
      </QueryClientProvider>
    );
    const waiting = render(ui("later_step"));
    expect(screen.getByText("Waiting to run")).toBeTruthy();
    waiting.unmount();
    const running = render(ui("running_step"));
    expect(screen.getByText("Running…")).toBeTruthy();
    const box = running.container.querySelector<HTMLElement>(
      "[data-testid=doc-step-live-thumbnail]",
    );
    expect(box === null || box.hidden).toBe(true);
  });

  it("says nothing about a finished run's steps until its rows have loaded, then says what happened", () => {
    const ui = (
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <DocStepResult label="get_invoices" />
        </MemoryRouter>
      </QueryClientProvider>
    );
    docRun.current = {
      runId: "wr_1",
      run: { downloaded_file_urls: [] },
      blocksByLabel: new Map(),
      live: false,
      rowsLoaded: false,
    };
    const loading = render(ui);
    expect(screen.queryByText(/Not reached/)).toBeNull();
    loading.unmount();
    docRun.current = { ...docRun.current, rowsLoaded: true };
    render(ui);
    expect(screen.getByText("Not reached")).toBeTruthy();
    cleanup();
    docRun.current = {
      ...docRun.current,
      ranLabels: new Set(["other_step"]),
    };
    render(ui);
    expect(screen.queryByText(/Not reached/)).toBeNull();
  });

  it("keeps a loop item's raw value off its line and behind a disclosure", () => {
    const loop = block({ block_type: "for_loop", label: "each_book" });
    docRun.current = {
      runId: "wr_1",
      run: { downloaded_file_urls: [] },
      blocksByLabel: new Map([["each_book", loop]]),
      loopsByLabel: new Map([
        [
          "each_book",
          {
            total: 1,
            items: [
              {
                index: 0,
                title: null,
                detail: "{'task_id': 'tsk_1', 'downloaded_file_urls': None}",
                state: "succeeded",
                failureReason: null,
              },
            ],
          },
        ],
      ]),
      live: true,
    };
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <DocStepResult label="each_book" />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const line = screen.getByRole("button", { name: "Show item 1" });
    expect(line.textContent).toBe("Item 1");
    expect(screen.queryByText(/tsk_1/)).toBeNull();
    fireEvent.click(line);
    expect(screen.getByTestId("doc-loop-item-value").textContent).toContain(
      "tsk_1",
    );
  });

  it("states a one-step run's failure on its step when the run failed before the step had a row", () => {
    docRun.current = {
      runId: "wr_1",
      run: {
        status: Status.Failed,
        failure_reason: "block 'get_invoices' has no starting URL set.",
        downloaded_file_urls: [],
      },
      blocksByLabel: new Map(),
      live: false,
      blockRun: true,
    };
    const ui = (label: string) => (
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter initialEntries={["/studio?wr=wr_1&bl=get_invoices"]}>
          <DocStepResult label={label} />
        </MemoryRouter>
      </QueryClientProvider>
    );
    render(ui("get_invoices"));
    expect(screen.getByTestId("doc-step-result").textContent).toMatch(
      /^Failed: /,
    );
    cleanup();
    render(ui("another_step"));
    expect(screen.queryByTestId("doc-step-result")).toBeNull();
  });

  it("shows nothing for a step an earlier run cannot speak for, even if that run failed it", () => {
    renderResult(block({ status: Status.Failed, failure_reason: "old error" }));
    expect(screen.getByTestId("doc-step-result")).toBeTruthy();
    cleanup();
    docRun.current = {
      ...docRun.current,
      unknownLabels: new Set(["get_invoices"]),
    };
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <DocStepResult label="get_invoices" />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.queryByTestId("doc-step-result")).toBeNull();
  });

  it("shows a Stop step that stopped the run as done, not failed", () => {
    renderResult(
      block({
        block_type: "terminate",
        label: "stop_here",
        status: Status.Terminated,
        failure_reason:
          "terminate block terminated. Reason: Nothing more to do",
        output: null,
      }),
    );
    const line = screen.getByText(/^Stopped/);
    expect(line.className).not.toContain("text-destructive");
  });

  it("draws a step of a run still going elsewhere as running, not stopped", () => {
    renderResult(block({ status: Status.Running }), true);
    expect(screen.queryByText(/^Stopped/)).toBeNull();
    expect(screen.queryByText(/Not reached/)).toBeNull();
  });

  it("states the outcome with its duration, links to the real run page and expands to the output", () => {
    renderResult(block({}));
    expect(screen.getByText("Found 3 items · 9s")).toBeTruthy();
    expect(
      screen.getByRole("link", { name: /open run/i }).getAttribute("href"),
    ).toBe("/runs/wr_1");
    expect(screen.queryByTestId("doc-step-values")).toBeNull();
    fireEvent.click(
      screen.getByRole("button", { name: /show result of get_invoices/i }),
    );
    expect(screen.getByTestId("doc-step-values")).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: "n" })).toBeTruthy();
    expect(screen.queryByText(/Summary|Inputs|Task ID|Engine/)).toBeNull();
  });

  it("counts the rows of an extraction wrapped in one key, matching its table", () => {
    renderResult(
      block({
        output: { extracted_information: { books: [{ n: 1 }, { n: 2 }] } },
      }),
    );
    expect(screen.getByText("Found 2 items · 9s")).toBeTruthy();
  });

  it("caps a long table at five rows and says how many more", () => {
    renderResult(
      block({
        output: {
          extracted_information: Array.from({ length: 8 }, (_, i) => ({
            title: `Book ${i}`,
            price: i,
          })),
        },
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: /show result of/i }));
    expect(screen.getAllByRole("row")).toHaveLength(1 + 5 + 1);
    expect(screen.getByText("3 more rows")).toBeTruthy();
  });

  it("gives a step with nothing to show no chevron and says it returned nothing", () => {
    renderResult(
      block({ block_type: "code", label: "run_code", output: null }),
    );
    expect(screen.queryByRole("button", { name: /result of/i })).toBeNull();
    expect(screen.getByText(/Completed · 9s · returned nothing/)).toBeTruthy();
  });

  it("shows only the item list under an expanded loop, never a screenshot", () => {
    screenshotMock.artifact = { artifact_id: "art_1", archived: false };
    const loop = block({
      block_type: "for_loop",
      label: "each_book",
      output: null,
    });
    docRun.current = {
      runId: "wr_1",
      run: { downloaded_file_urls: [] },
      blocksByLabel: new Map([["each_book", loop]]),
      loopsByLabel: new Map([
        [
          "each_book",
          {
            total: 1,
            items: [
              {
                index: 0,
                title: "Attic",
                detail: null,
                state: "succeeded",
                failureHeadline: null,
              },
            ],
          },
        ],
      ]),
      live: false,
    };
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <DocStepResult label="each_book" />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    fireEvent.click(
      screen.getByRole("button", { name: /show result of each_book/i }),
    );
    expect(screen.getByTestId("doc-loop-items")).toBeTruthy();
    expect(screen.queryByTestId("doc-step-last-screen")).toBeNull();
  });

  it("counts a loop's failed items in red and lists the failed one with its reason", () => {
    const loop = block({
      block_type: "for_loop",
      label: "each_book",
      output: null,
    });
    docRun.current = {
      runId: "wr_1",
      run: { downloaded_file_urls: [] },
      blocksByLabel: new Map([["each_book", loop]]),
      loopsByLabel: new Map([
        [
          "each_book",
          {
            total: 2,
            items: [
              {
                index: 0,
                title: "Attic",
                detail: null,
                state: "succeeded",
                failureHeadline: null,
              },
              {
                index: 1,
                title: "Velvet",
                detail: null,
                state: "failed",
                failureHeadline: "the page did not load",
              },
            ],
          },
        ],
      ]),
      live: false,
    };
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <DocStepResult label="each_book" />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.getByText("· 1 failed").className).toContain("text-red-700");
    fireEvent.click(
      screen.getByRole("button", { name: /show result of each_book/i }),
    );
    expect(
      screen
        .getByText("Item 2 · Velvet · Failed: the page did not load")
        .closest("li")!.className,
    ).toContain("text-red-700");
  });

  it("says a step inside a loop ran N times, never one item's result as the whole step's", () => {
    docRun.current = {
      runId: "wr_1",
      run: { downloaded_file_urls: [] },
      blocksByLabel: new Map([["get_invoices", block({})]]),
      runsByLabel: new Map([
        ["get_invoices", { count: 3, failed: 1, withOutput: 2 }],
      ]),
      live: false,
    };
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <DocStepResult label="get_invoices" />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.getByText("· 1 failed")).toBeTruthy();
    expect(screen.getByText(/^Ran 3 times/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /result of/i })).toBeNull();
  });

  it("never says a looped Code step returned nothing when its runs returned values", () => {
    const ranTwice = (withOutput: number) => {
      const b = block({
        block_type: "code",
        output: { result: "under budget" },
      });
      docRun.current = {
        runId: "wr_1",
        run: { downloaded_file_urls: [] },
        blocksByLabel: new Map([[b.label as string, b]]),
        runsByLabel: new Map([
          [b.label as string, { count: 2, failed: 0, withOutput }],
        ]),
        live: false,
      };
      return render(
        <QueryClientProvider client={new QueryClient()}>
          <MemoryRouter>
            <DocStepResult label={b.label as string} />
          </MemoryRouter>
        </QueryClientProvider>,
      );
    };
    const returned = ranTwice(2);
    expect(returned.getByTestId("doc-step-result").textContent).toContain(
      "Ran 2 times",
    );
    expect(returned.queryByText(/returned nothing/)).toBeNull();
    returned.unmount();
    // Only when no run returned anything does the line say so.
    const empty = ranTwice(0);
    expect(empty.getByText(/Ran 2 times · returned nothing/)).toBeTruthy();
  });

  it("says a Code step that returned nothing did so, ignoring the screen taken before it ran", () => {
    screenshotMock.artifact = {
      artifact_id: "art_code",
      artifact_type: ArtifactType.LLMScreenshot,
      archived: false,
    };
    const before = renderResult(block({ block_type: "code", output: null }));
    expect(screen.queryByRole("button", { name: /result of/i })).toBeNull();
    expect(screen.getByText(/Completed · 9s · returned nothing/)).toBeTruthy();
    before.unmount();
    // A screen from the Code step's own page actions is its result.
    screenshotMock.artifact = {
      artifact_id: "art_code_action",
      artifact_type: ArtifactType.ActionScreenshot,
      archived: false,
    };
    renderResult(block({ block_type: "code", output: null }));
    fireEvent.load(screen.getByTestId("doc-step-last-screen"));
    expect(
      screen.getByRole("button", { name: /show result of/i }),
    ).toBeTruthy();
    screenshotMock.artifact = undefined;
  });

  it("reads Canceled on a loop the run was canceled in, not just its item count", () => {
    const loop = block({
      block_type: "for_loop",
      label: "each_book",
      status: Status.Canceled,
      output: null,
    });
    docRun.current = {
      runId: "wr_1",
      run: { status: Status.Canceled, downloaded_file_urls: [] },
      blocksByLabel: new Map([["each_book", loop]]),
      loopsByLabel: new Map([
        [
          "each_book",
          {
            total: 3,
            items: [
              { index: 0, state: "completed" },
              { index: 1, state: "completed" },
            ],
          },
        ],
      ]),
      live: false,
    };
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <DocStepResult label="each_book" />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(
      screen.getByRole("button", { name: /show result of each_book/i })
        .textContent,
    ).toMatch(/^Canceled/);
    expect(screen.getByRole("img", { name: "canceled" })).toBeTruthy();
  });

  it("keeps the reason of a step that ran several times and ended the run failed", () => {
    const b = block({
      status: Status.Failed,
      failure_reason:
        "The page never finished loading. The site kept redirecting.",
    });
    docRun.current = {
      runId: "wr_1",
      run: { downloaded_file_urls: [] },
      blocksByLabel: new Map([[b.label as string, b]]),
      runsByLabel: new Map([
        [b.label as string, { count: 3, failed: 1, withOutput: 2 }],
      ]),
      live: false,
    };
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <DocStepResult label="get_invoices" />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: /show result of/i }));
    expect(screen.getByText(/kept redirecting/)).toBeTruthy();
  });

  it("reads Canceled for a step left running when the run was canceled", () => {
    const b = block({ status: Status.Running });
    docRun.current = {
      runId: "wr_1",
      run: { status: Status.Canceled, downloaded_file_urls: [] },
      blocksByLabel: new Map([[b.label as string, b]]),
      live: false,
    };
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <DocStepResult label="get_invoices" />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.getByText(/^Canceled/)).toBeTruthy();
    expect(screen.queryByText(/^Stopped/)).toBeNull();
  });

  it("lists a long loop's failed items first and caps the list at five", () => {
    const loop = block({
      block_type: "for_loop",
      label: "each_book",
      output: null,
    });
    docRun.current = {
      runId: "wr_1",
      run: { downloaded_file_urls: [] },
      blocksByLabel: new Map([["each_book", loop]]),
      loopsByLabel: new Map([
        [
          "each_book",
          {
            total: 8,
            items: Array.from({ length: 8 }, (_, index) => ({
              index,
              title: `b${index}`,
              detail: null,
              state: index === 7 ? "failed" : "succeeded",
              failureHeadline: index === 7 ? "it broke" : null,
            })),
          },
        ],
      ]),
      live: false,
    };
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <DocStepResult label="each_book" />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    fireEvent.click(
      screen.getByRole("button", { name: /show result of each_book/i }),
    );
    const items = screen.getByTestId("doc-loop-items").querySelectorAll("li");
    expect(items).toHaveLength(6);
    expect(items[0]!.textContent).toContain("Item 8");
    expect(screen.getByText("3 more items")).toBeTruthy();
  });

  it("draws a canceled step grey, never red", () => {
    const { container } = renderResult(block({ status: Status.Canceled }));
    expect(screen.getByText(/^Canceled/).className).not.toContain(
      "text-destructive",
    );
    expect(container.querySelector(".text-destructive")).toBeNull();
  });

  it("shows a failed step's longer reason in its expansion, with no tabs", () => {
    renderResult(
      block({
        status: Status.Failed,
        failure_reason:
          "The page never finished loading. The site kept redirecting to a sign-in page.",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: /show result of/i }));
    expect(screen.getByText(/kept redirecting/)).toBeTruthy();
    expect(screen.queryByRole("tab")).toBeNull();
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
  const renderConditional = (
    executed: string | null | undefined,
    openBranch?: string,
    opts: { skippedSteps?: number; edited?: boolean } = {},
  ) => {
    const shown = {
      ...item,
      branches: item.branches.map((b) =>
        b.kind === "if" ? { ...b, stepCount: opts.skippedSteps ?? 0 } : b,
      ),
    } as typeof item;
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
      unknownLabels: new Set(opts.edited ? ["check_box"] : []),
    };
    return render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <WorkflowDocActionsContext.Consumer>
            {(actions) => (
              <WorkflowDocActionsContext.Provider
                value={{ ...actions, selectedBranch: () => openBranch }}
              >
                <DocConditional
                  node={conditional}
                  item={shown}
                  byId={new Map()}
                  editable={false}
                  renderChildren={() => null}
                />
              </WorkflowDocActionsContext.Provider>
            )}
          </WorkflowDocActionsContext.Consumer>
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

  it("says in plain words which branch was taken and that the other's steps were skipped", () => {
    renderConditional("b_else", "b_if", { skippedSteps: 2 });
    expect(screen.getByTestId("doc-step-result").textContent).toMatch(
      /Took Otherwise/,
    );
    expect(screen.getByTestId("doc-branch-skipped").textContent).toBe(
      "No, so these steps were skipped",
    );
  });

  it("drops the ran badge and both plain-words lines once the conditional is edited since the run", () => {
    renderConditional("b_else", "b_if", { skippedSteps: 2, edited: true });
    expect(screen.queryByTestId("doc-step-result")).toBeNull();
    expect(screen.queryByTestId("doc-branch-skipped")).toBeNull();
    expect(screen.queryByText("ran")).toBeNull();
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

  it("does not call a branch skipped when its steps ran on an earlier item of a loop", () => {
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
            executed_branch_id: "b_else",
          }),
        ],
        ["tick_on", block({ label: "tick_on" })],
      ]),
      live: false,
      unknownLabels: new Set<string>(),
    };
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <WorkflowDocActionsContext.Consumer>
            {(actions) => (
              <WorkflowDocActionsContext.Provider
                value={{ ...actions, selectedBranch: () => "b_if" }}
              >
                <DocConditional
                  node={conditional}
                  item={
                    {
                      ...item,
                      branches: [
                        {
                          ...item.branches[0]!,
                          stepCount: 1,
                          children: [{ kind: "step", nodeId: "t1" }],
                        },
                        item.branches[1]!,
                      ],
                    } as typeof item
                  }
                  byId={new Map([["t1", tick]])}
                  editable={false}
                  renderChildren={() => null}
                />
              </WorkflowDocActionsContext.Provider>
            )}
          </WorkflowDocActionsContext.Consumer>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.queryByTestId("doc-branch-skipped")).toBeNull();
  });

  it("names the branch from the condition the run recorded when a save renamed the branch IDs", () => {
    const run = (expression: string | null, result?: boolean | null) => {
      docRun.current = {
        runId: "wr_1",
        blocksByLabel: new Map([
          [
            "check_box",
            block({
              label: "check_box",
              block_type: "conditional",
              executed_branch_id: "id_minted_by_the_save",
              executed_branch_expression: expression,
              executed_branch_result: result,
            }),
          ],
        ]),
        live: false,
        unknownLabels: new Set<string>(),
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
    run(null, false);
    expect(screen.getByTestId("doc-step-result").textContent).toMatch(
      /Took Otherwise/,
    );
    cleanup();
    run(null, null);
    expect(screen.getByTestId("doc-step-result").textContent).toMatch(
      /Took Otherwise/,
    );
    cleanup();
    run(null);
    expect(tab(/otherwise/i).textContent).not.toMatch(/ran/);
    cleanup();
    run("{{ checked }}");
    expect(tab(/^if/i).textContent).toMatch(/ran/);
    expect(tab(/otherwise/i).textContent).not.toMatch(/ran/);
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
