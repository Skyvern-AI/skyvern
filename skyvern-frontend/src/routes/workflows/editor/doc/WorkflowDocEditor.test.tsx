// @vitest-environment jsdom
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ReactFlowProvider } from "@xyflow/react";
import { MemoryRouter } from "react-router-dom";

import { ProxyLocation, Status } from "@/api/types";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";

import type {
  WorkflowBlock,
  WorkflowSettings,
} from "../../types/workflowTypes";
import { isWorkflowBlockNode } from "../nodes";
import { getElements } from "../workflowEditorUtils";
import { useLocateBlockStore } from "../runValidation/useLocateBlockStore";
import { WorkflowDocEditor } from "./WorkflowDocEditor";
import { block, conditional, forLoop, settings } from "./docTestBlocks";
import type { DocGraphEdits } from "./docEditApi";
import { useWorkflowYamlEditorStore } from "@/store/WorkflowYamlEditorStore";

const docRun = vi.hoisted(() => ({ current: {} as Record<string, unknown> }));
vi.mock("./useDocRun", () => ({ useDocRun: () => docRun.current }));
import type { DocGraph } from "./docGraphEdits";
import * as axiosClientModule from "@/api/AxiosClient";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";

vi.mock("../workflowYamlDocument", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../workflowYamlDocument")>()),
  buildWorkflowSaveRequest: () => ({}),
}));

// The run widgets need studio run contexts; this file tests navigation, so they are stubbed.
vi.mock("./DocStepResult", () => ({ DocStepResult: () => null }));
vi.mock("./DocLiveIndicator", () => ({ DocLiveIndicator: () => null }));
vi.mock("./DocRunStepButton", () => ({ DocRunStepButton: () => null }));

const output = (label: string) => ({
  parameter_type: "output",
  key: `${label}_output`,
  output_parameter_id: `op-${label}`,
});
const code = (label: string, next: string | null) =>
  ({
    label,
    block_type: "code",
    next_block_label: next,
    continue_on_failure: false,
    model: null,
    output_parameter: output(label),
    code: "",
    parameters: [],
  }) as unknown as WorkflowBlock;
const loop = {
  label: "each_row",
  block_type: "for_loop",
  next_block_label: null,
  continue_on_failure: false,
  model: null,
  output_parameter: output("each_row"),
  loop_over: { key: "items" },
  loop_blocks: [code("inner_step", null)],
  loop_variable_reference: "{{ items }}",
  complete_if_empty: false,
} as unknown as WorkflowBlock;

function renderDoc(blocks: WorkflowBlock[] = [loop], edits?: DocGraphEdits) {
  const { nodes, edges } = getElements(
    blocks,
    {
      proxyLocation: ProxyLocation.Residential,
      finallyBlockLabel: null,
    } as unknown as WorkflowSettings,
    true,
  );
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <ReactFlowProvider>
          <WorkflowDocEditor nodes={nodes} edges={edges} edits={edits} />
        </ReactFlowProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return nodes;
}

describe("WorkflowDocEditor navigation", () => {
  beforeEach(() => {
    docRun.current = { runId: null, blocksByLabel: new Map(), live: false };
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    useWorkflowParametersStore.setState({ parameters: [] });
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        disconnect() {}
      },
    );
    vi.stubGlobal("requestAnimationFrame", (cb: () => void) => {
      cb();
      return 0;
    });
    vi.stubGlobal("CSS", { escape: (value: string) => value });
    Element.prototype.scrollIntoView = vi.fn();
  });
  afterEach(() => {
    cleanup();
    useLocateBlockStore.getState().clearLocate();
    vi.unstubAllGlobals();
  });

  test("Tab nests into the run branch displayed on screen without a manual selection", () => {
    docRun.current = {
      runId: "wr_1",
      blocksByLabel: new Map([
        [
          "route",
          { status: Status.Completed, executed_branch_id: "old_branch" },
        ],
        ["else_step", { status: Status.Completed }],
      ]),
      live: false,
    };
    const edits: DocGraphEdits = {
      apply: vi.fn(),
      update: vi.fn(),
      addStep: vi.fn(),
      duplicate: vi.fn(),
    };
    const nodes = renderDoc(
      [
        conditional("route", "after", [
          { id: "yes", next: "if_step", when: "Check the condition" },
          { id: "no", next: "else_step" },
        ]),
        code("if_step", "after"),
        code("else_step", "after"),
        block("text_prompt", "after", null, { prompt: "Do the next thing" }),
      ],
      edits,
    );
    expect(
      screen
        .getByRole("tab", { name: /Otherwise/ })
        .getAttribute("aria-selected"),
    ).toBe("true");
    fireEvent.focus(screen.getByRole("textbox", { name: "after prompt" }));
    fireEvent.keyDown(screen.getByRole("textbox", { name: "after prompt" }), {
      key: "Tab",
    });

    expect(edits.apply).toHaveBeenCalledOnce();
    const moved = vi
      .mocked(edits.apply)
      .mock.calls[0]![0].nodes.filter(isWorkflowBlockNode)
      .find((n) => n.data.label === "after")!;
    const route = nodes.find((n) => n.type === "conditional")!;
    expect(moved.parentId).toBe(route.id);
    expect(moved.data.conditionalBranchId).toBe(
      route.data.branches.find((b) => b.is_default)!.id,
    );
  });

  test("Locate on a step inside a collapsed loop reopens the loop and consumes the request", () => {
    const nodes = renderDoc();
    const inner = nodes.find((n) => n.data.label === "inner_step")!;
    expect(
      document.querySelector(`[data-doc-node-id="${inner.id}"]`),
    ).not.toBeNull();

    const toggle = screen
      .getAllByRole("button")
      .find((b) => b.getAttribute("aria-expanded") === "true")!;
    fireEvent.click(toggle);
    expect(
      document.querySelector(`[data-doc-node-id="${inner.id}"]`),
    ).toBeNull();

    act(() => useLocateBlockStore.getState().requestLocate(inner.id));

    expect(
      document.querySelector(`[data-doc-node-id="${inner.id}"]`),
    ).not.toBeNull();
    expect(useLocateBlockStore.getState().request).toBeNull();
  });

  test("counts server validation problems in the edit bar", async () => {
    vi.useFakeTimers();
    const post = vi.fn().mockResolvedValue({
      data: {
        problems: [
          {
            code: "unknown_reference",
            block_label: "inner_step",
            message: "Unknown reference: missing_value.",
          },
          {
            code: "schema_error",
            block_label: "inner_step",
            message: "This block has an invalid setting.",
          },
        ],
      },
    });
    vi.spyOn(axiosClientModule, "getClient").mockResolvedValue({
      post,
    } as never);
    const getSaveData = useWorkflowHasChangesStore.getState().getSaveData;
    useWorkflowHasChangesStore.setState({ getSaveData: () => ({}) as never });

    try {
      renderDoc();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });

      expect(screen.getByText("2 problems")).toBeTruthy();
      expect(
        screen.getByText(/This block has an invalid setting\./),
      ).toBeTruthy();
    } finally {
      useWorkflowHasChangesStore.setState({ getSaveData });
      vi.useRealTimers();
      vi.restoreAllMocks();
      cleanup();
    }
  });

  test("shows a 413 validation failure without dropping the previous credential warning", async () => {
    vi.useFakeTimers();
    const post = vi
      .fn()
      .mockResolvedValueOnce({
        data: {
          problems: [
            {
              code: "credential_in_note",
              block_label: "inner_step",
              field: "notes",
              message: "Notes can't use a credential input.",
            },
          ],
        },
      })
      .mockRejectedValue({
        isAxiosError: true,
        response: {
          status: 413,
          data: {
            detail: "Workflow validation drafts must be at most 100000 bytes.",
          },
        },
      });
    vi.spyOn(axiosClientModule, "getClient").mockResolvedValue({
      post,
    } as never);
    const getSaveData = useWorkflowHasChangesStore.getState().getSaveData;
    useWorkflowHasChangesStore.setState({ getSaveData: () => ({}) as never });
    try {
      renderDoc();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
      expect(screen.getByText("1 problem")).toBeTruthy();
      act(() => useWorkflowParametersStore.setState({ parameters: [] }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
      expect(screen.getByRole("alert").textContent).toContain(
        "Couldn't check this workflow: Workflow validation drafts must be at most 100000 bytes.",
      );
      expect(screen.getByText("2 problems")).toBeTruthy();
    } finally {
      useWorkflowHasChangesStore.setState({ getSaveData });
      vi.useRealTimers();
      vi.restoreAllMocks();
      cleanup();
    }
  });

  test("shows workflow-level validation problems", async () => {
    vi.useFakeTimers();
    const post = vi.fn().mockResolvedValue({
      data: {
        valid: false,
        problems: [
          {
            block_label: null,
            code: "validation_limit",
            message: "Workflow validation supports at most 500 blocks.",
          },
        ],
      },
    });
    vi.spyOn(axiosClientModule, "getClient").mockResolvedValue({
      post,
    } as never);
    const getSaveData = useWorkflowHasChangesStore.getState().getSaveData;
    useWorkflowHasChangesStore.setState({ getSaveData: () => ({}) as never });

    try {
      renderDoc();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });

      expect(screen.getByRole("alert").textContent).toContain(
        "Workflow validation supports at most 500 blocks.",
      );
      expect(screen.getByText("1 problem")).toBeTruthy();
    } finally {
      useWorkflowHasChangesStore.setState({ getSaveData });
      vi.useRealTimers();
      vi.restoreAllMocks();
      cleanup();
    }
  });

  test("Tab reveals a moved step after the asynchronous graph update reaches a collapsed loop", () => {
    const graph = getElements(
      [
        forLoop("each", "after", [code("inner", null)]),
        block("text_prompt", "after", null, {
          prompt: "Write a summary",
          parameters: [],
        }),
      ],
      settings(),
      true,
    ) as DocGraph;
    const moved = graph.nodes.find((node) => node.data.label === "after")!;
    const parent = graph.nodes.find((node) => node.data.label === "each")!;
    let pending = graph;
    const client = new QueryClient();
    const edits = {
      apply: (next: DocGraph) => {
        pending = next;
      },
      update: () => {},
      addStep: () => null,
      duplicate: () => {},
    };
    const view = (current: DocGraph) => (
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <ReactFlowProvider>
            <WorkflowDocEditor {...current} edits={edits} />
          </ReactFlowProvider>
        </MemoryRouter>
      </QueryClientProvider>
    );
    const rendered = render(view(graph));
    const toggle = screen
      .getAllByRole("button")
      .find((button) => button.getAttribute("aria-expanded") === "true")!;
    fireEvent.click(toggle);
    fireEvent.click(screen.getByText("Write a summary"));
    const prompt = screen.getByRole("textbox", { name: "after prompt" });

    fireEvent.keyDown(prompt, { key: "Tab" });

    expect(pending.nodes.find((node) => node.id === moved.id)?.parentId).toBe(
      parent.id,
    );
    expect(useLocateBlockStore.getState().request?.nodeId).toBe(moved.id);
    rendered.rerender(view(pending));
    expect(
      document.querySelector(`[data-doc-node-id="${moved.id}"]`),
    ).not.toBeNull();
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(useLocateBlockStore.getState().request).toBeNull();
  });
});

test("a step that reads an output only some branches produce shows the warning under it", async () => {
  vi.useFakeTimers();
  const post = vi.fn().mockResolvedValue({
    data: {
      valid: true,
      problems: [
        {
          code: "branch_only_reference",
          severity: "warning",
          block_label: "inner_step",
          message: "left_output is only produced on some branches of check.",
        },
        {
          code: "forward_reference",
          severity: "error",
          block_label: "inner_step",
          message: "later_output is produced by a later step.",
        },
        {
          code: "unknown_reference",
          severity: "error",
          block_label: "inner_step",
          message: "Unknown reference: missing_value.",
        },
      ],
    },
  });
  vi.spyOn(axiosClientModule, "getClient").mockResolvedValue({
    post,
  } as never);
  const getSaveData = useWorkflowHasChangesStore.getState().getSaveData;
  useWorkflowHasChangesStore.setState({ getSaveData: () => ({}) as never });
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  try {
    renderDoc();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    const stepProblem = screen.getByText(
      /left_output is only produced on some branches of check\./,
    );
    expect(stepProblem.textContent).toContain(
      "later_output is produced by a later step.",
    );
    expect(stepProblem.textContent).toContain(
      "Unknown reference: missing_value.",
    );
    // A warning on a step is not a note that needs fixing.
    expect(screen.queryByText(/needs? fixing/)).toBeNull();
  } finally {
    useWorkflowHasChangesStore.setState({ getSaveData });
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    cleanup();
  }
});
