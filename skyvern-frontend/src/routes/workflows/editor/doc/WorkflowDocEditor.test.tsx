// @vitest-environment jsdom
import { useRunValidationStore } from "../runValidation/useRunValidationStore";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ReactFlowProvider } from "@xyflow/react";
import { MemoryRouter } from "react-router-dom";

import { ProxyLocation, Status } from "@/api/types";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";

import {
  WorkflowParameterValueType,
  type WorkflowBlock,
  type WorkflowSettings,
} from "../../types/workflowTypes";
import { isWorkflowBlockNode } from "../nodes";
import { getElements } from "../workflowEditorUtils";
import { useLocateBlockStore } from "../runValidation/useLocateBlockStore";
import { WorkflowDocEditor } from "./WorkflowDocEditor";
import { useDocUnfinishedStore } from "./docUnfinished";
import { splitAtCaret } from "./docCaret";
import { block, conditional, forLoop, settings } from "./docTestBlocks";
import type { DocGraphEdits } from "./docEditApi";
import { useWorkflowYamlEditorStore } from "@/store/WorkflowYamlEditorStore";

const docRun = vi.hoisted(() => ({ current: {} as Record<string, unknown> }));
vi.mock("./useDocRun", () => ({
  useDocRun: () => ({ unknownLabels: new Set(), ...docRun.current }),
}));
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
        unobserve() {}
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

  describe("keyboard-first editing", () => {
    const editsWithAdd = (): DocGraphEdits => ({
      apply: vi.fn(),
      update: vi.fn(),
      addStep: vi.fn(() => "new_step"),
      duplicate: vi.fn(),
    });
    // Opens the line and puts the caret `at` characters in; jsdom has no innerText, so the text is set both ways.
    const openLine = (name: string, text: string, at: number) => {
      fireEvent.focus(screen.getByRole("textbox", { name }));
      const box = screen.getByRole("textbox", { name });
      Object.defineProperty(box, "innerText", {
        value: text,
        configurable: true,
      });
      box.textContent = text;
      const range = document.createRange();
      if (box.firstChild) range.setStart(box.firstChild, at);
      else range.setStart(box, 0);
      range.collapse(true);
      window.getSelection()?.removeAllRanges();
      window.getSelection()?.addRange(range);
      return box;
    };
    const prompt = () =>
      block("text_prompt", "write_it", null, { prompt: "Do the next thing" });

    test("Enter at the end of a step adds a step of the same type below", () => {
      const edits = editsWithAdd();
      const nodes = renderDoc([prompt()], edits);
      const box = openLine("write_it prompt", "Do the next thing", 17);
      fireEvent.keyDown(box, { key: "Enter" });

      expect(edits.addStep).toHaveBeenCalledTimes(1);
      expect(vi.mocked(edits.addStep).mock.calls[0]![0].nodeType).toBe(
        nodes.find(isWorkflowBlockNode)!.type,
      );
    });

    test("Enter in the middle of a step splits its text between the two steps", () => {
      const edits = editsWithAdd();
      const nodes = renderDoc([prompt()], edits);
      const box = openLine("write_it prompt", "Do the next thing", 6);
      fireEvent.keyDown(box, { key: "Enter" });

      expect(edits.addStep).toHaveBeenCalledTimes(1);
      const updates = vi.mocked(edits.update).mock.calls;
      const last = updates[updates.length - 1]![0];
      const first = last(nodes).find(isWorkflowBlockNode)!.data as Record<
        string,
        unknown
      >;
      expect(first.prompt).toBe("Do the");
    });

    test("Shift+Enter is a line break inside the step, not a new step", () => {
      const edits = editsWithAdd();
      renderDoc([prompt()], edits);
      const box = openLine("write_it prompt", "Do the next thing", 17);
      const proceeded = fireEvent.keyDown(box, {
        key: "Enter",
        shiftKey: true,
      });

      expect(proceeded).toBe(true);
      expect(edits.addStep).not.toHaveBeenCalled();
    });

    test("a save while the caret is in a step leaves its text on screen", () => {
      // jsdom has no innerText; the line reads and writes its text through it.
      const innerText = Object.getOwnPropertyDescriptor(
        HTMLElement.prototype,
        "innerText",
      );
      Object.defineProperty(HTMLElement.prototype, "innerText", {
        configurable: true,
        get(this: HTMLElement) {
          return this.textContent ?? "";
        },
        set(this: HTMLElement, text: string) {
          this.textContent = text;
        },
      });
      try {
        const goal = "Find the book and return its price";
        renderDoc(
          [
            block("extraction", "find_it", null, {
              data_extraction_goal: goal,
            }),
          ],
          editsWithAdd(),
        );
        fireEvent.focus(
          screen.getByRole("textbox", { name: "find_it dataExtractionGoal" }),
        );
        const typing = screen.getByRole("textbox", {
          name: "find_it dataExtractionGoal",
        });
        expect(typing.textContent).toBe(goal);
        const caret = document.createRange();
        caret.setStart(typing.firstChild!, "Find the ".length);
        caret.setEnd(typing.firstChild!, "Find the book".length);
        window.getSelection()?.removeAllRanges();
        window.getSelection()?.addRange(caret);
        fireEvent.keyDown(typing, { key: "s", metaKey: true });

        // Saving holds the editor lock until the request settles.
        act(() =>
          useWorkflowYamlEditorStore.setState({ commitInProgress: true }),
        );
        act(() =>
          useWorkflowYamlEditorStore.setState({ commitInProgress: false }),
        );

        const line = screen.getByRole("textbox", {
          name: "find_it dataExtractionGoal",
        });
        expect(line.textContent).toBe(goal);
        expect(document.activeElement).toBe(line);
        expect(splitAtCaret(line)).toEqual([
          "Find the ",
          " and return its price",
        ]);
      } finally {
        if (innerText)
          Object.defineProperty(HTMLElement.prototype, "innerText", innerText);
        else delete (HTMLElement.prototype as { innerText?: string }).innerText;
      }
    });

    test("Tab on a note under an empty loop opens the step menu and picks no type", () => {
      const edits = editsWithAdd();
      renderDoc(
        [
          forLoop("books", null, [], {
            trailing_notes: "Open each book page",
          }),
        ],
        edits,
      );
      const box = openLine("Note", "Open each book page", 19);
      fireEvent.keyDown(box, { key: "Tab" });

      expect(screen.getByPlaceholderText("Add a step…")).toBeTruthy();
      expect(edits.addStep).not.toHaveBeenCalled();
    });
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

  test.each([
    [
      "code",
      { code: "", parameters: [] },
      "Run Python code",
      "prompt",
      "Get the top five posts",
    ],
    ["goto_url", { url: "" }, "Go to a URL", "url", "https://example.com/"],
    [
      "email_inbox",
      { prompt: "" },
      "Find an email",
      "prompt",
      "Latest invoice",
    ],
  ])(
    "typing in a %s step's line edits the field that drives it, not its description",
    (blockType, extra, placeholder, key, typed) => {
      const edits: DocGraphEdits = {
        apply: vi.fn(),
        update: vi.fn(),
        addStep: vi.fn(),
        duplicate: vi.fn(),
      };
      const nodes = renderDoc(
        [block(blockType, "the_step", null, extra)],
        edits,
      );
      expect(screen.getByText(placeholder)).toBeTruthy();
      const name = `the_step ${key}`;
      fireEvent.focus(screen.getByRole("textbox", { name }));
      const box = screen.getByRole("textbox", { name });
      // jsdom has no innerText; the line reads it on input.
      Object.defineProperty(box, "innerText", {
        value: typed,
        configurable: true,
      });
      fireEvent.input(box);

      const updater = vi.mocked(edits.update).mock.calls[0]![0];
      const data = updater(nodes).find(isWorkflowBlockNode)!.data as Record<
        string,
        unknown
      >;
      expect(data[key]).toBe(typed);
      expect(data.description).not.toBe(typed);
    },
  );

  test("a read-only doc reserves no empty row above Inputs", () => {
    renderDoc([block("goto_url", "open_site", null, { url: "x" })]);
    expect(document.querySelector('[aria-hidden="true"].h-\\[28px\\]')).toBe(
      null,
    );
  });

  test("a Go to URL step with no URL shows a required chip", () => {
    renderDoc([block("goto_url", "open_site", null, { url: "" })]);
    expect(document.querySelector('[data-doc-missing="URL"]')).toBeTruthy();
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

  test("jumping to a step flashes it", async () => {
    const animate = vi.fn();
    HTMLElement.prototype.animate = animate;
    vi.stubGlobal("matchMedia", () => ({ matches: false }));
    vi.stubGlobal("requestAnimationFrame", (cb: () => void) => cb());
    const nodes = renderDoc();
    const inner = nodes.find((n) => n.data.label === "inner_step")!;
    act(() => useLocateBlockStore.getState().requestLocate(inner.id));
    expect(animate).toHaveBeenCalledTimes(1);
    // @ts-expect-error cleanup of the jsdom stub
    delete HTMLElement.prototype.animate;
  });

  test("opens step search from Ctrl+K anywhere in the document", () => {
    renderDoc([
      block("goto_url", "find_by_label", null, {
        description: "Open the internal portal",
        url: "https://example.test/unique-path",
      }),
    ]);
    fireEvent.keyDown(
      screen.getByRole("region", { name: "Workflow document" }),
      {
        key: "k",
        ctrlKey: true,
      },
    );

    const input = screen.getByPlaceholderText("Search steps…");
    fireEvent.change(input, { target: { value: "find_by_label" } });
    expect(screen.getAllByRole("option")).toHaveLength(1);
  });

  test("the branch actions button shows only when the branch has an action", () => {
    const edits: DocGraphEdits = {
      apply: vi.fn(),
      update: vi.fn(),
      addStep: vi.fn(),
      duplicate: vi.fn(),
    };
    renderDoc(
      [
        conditional("route", null, [
          { id: "yes", next: "if_step", when: "found" },
          { id: "otherwise", next: null },
        ]),
        code("if_step", null),
      ],
      edits,
    );
    expect(screen.queryByRole("button", { name: /Branch actions/ })).toBeNull();
  });

  test("collapse all closes loops and conditionals", () => {
    const nodes = renderDoc([
      forLoop("each_row", "route", [code("inner_loop", null)]),
      conditional("route", null, [
        { id: "yes", next: "inner_branch", when: "found" },
        { id: "otherwise", next: null },
      ]),
      code("inner_branch", null),
    ]);
    const loopChild = nodes.find((node) => node.data.label === "inner_loop")!;
    const branchChild = nodes.find(
      (node) => node.data.label === "inner_branch",
    )!;

    fireEvent.click(screen.getByRole("button", { name: "Collapse all" }));

    expect(
      document.querySelector(`[data-doc-node-id="${loopChild.id}"]`),
    ).toBeNull();
    expect(
      document.querySelector(`[data-doc-node-id="${branchChild.id}"]`),
    ).toBeNull();
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

  test("a credential named in a note holds Run even though the server sends it as a warning", async () => {
    vi.useFakeTimers();
    const post = vi.fn().mockResolvedValue({
      data: {
        problems: [
          {
            code: "credential_in_note",
            severity: "warning",
            block_label: "inner_step",
            field: "notes",
            message: "Notes can't use a credential input.",
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
      expect(useRunValidationStore.getState().docProblemCount).toBe(1);
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
      expect(useRunValidationStore.getState().docProblemCount).toBe(1);
      act(() => useWorkflowParametersStore.setState({ parameters: [] }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
      expect(screen.getByRole("alert").textContent).toContain(
        "Couldn't check this workflow: Workflow validation drafts must be at most 100000 bytes.",
      );
      expect(screen.getByText("2 problems")).toBeTruthy();
      // The earlier check's warning describes an older draft, so it no longer holds the run.
      expect(useRunValidationStore.getState().docProblemCount).toBe(0);
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

  test("a warning-only validation response shows the problem but doesn't hold Run", async () => {
    vi.useFakeTimers();
    const post = vi.fn().mockResolvedValue({
      data: {
        valid: true,
        problems: [
          {
            block_label: null,
            code: "unknown_loop_source",
            severity: "warning",
            message: "The loop source isn't a known input.",
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
      expect(screen.getByText("1 problem")).toBeTruthy();
      expect(useRunValidationStore.getState().docProblemCount).toBe(0);
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

describe("a step that was just added", () => {
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
        unobserve() {}
        disconnect() {}
      },
    );
    vi.stubGlobal("CSS", { escape: (value: string) => value });
  });
  afterEach(() => {
    cleanup();
    useDocUnfinishedStore.getState().clear();
    vi.unstubAllGlobals();
  });

  test("shows its missing setting once, and only after the user leaves it", () => {
    const nodes = renderDoc([block("login", "sign_in", null, { url: "" })]);
    const login = nodes.find((n) => isWorkflowBlockNode(n))!;
    act(() => useDocUnfinishedStore.getState().add(login.id));
    expect(document.querySelector("[data-doc-step-fix]")).toBeNull();
    expect(document.querySelector("[data-doc-problems]")).toBeNull();
    expect(useRunValidationStore.getState().docProblemCount).toBe(0);

    fireEvent.pointerDown(document.body);

    expect(
      screen.getAllByText("Choose a saved credential in this step's settings"),
    ).toHaveLength(1);
    expect(document.querySelector("[data-doc-problems]")?.textContent).toBe(
      "1 problem",
    );
    expect(screen.queryByText(/need.* fixing/)).toBeNull();
  });

  test("a click inside a floating menu does not count as leaving the new step", () => {
    const nodes = renderDoc([block("login", "sign_in", null, { url: "" })]);
    const login = nodes.find((n) => isWorkflowBlockNode(n))!;
    act(() => useDocUnfinishedStore.getState().add(login.id));
    const option = document.body.appendChild(document.createElement("div"));
    option.setAttribute("role", "listbox");
    fireEvent.pointerDown(option);
    expect(document.querySelector("[data-doc-problems]")).toBeNull();
    option.remove();
  });
});

describe("A Browser Action that needs several actions", () => {
  const actionStep = {
    label: "search_step",
    block_type: "action",
    next_block_label: null,
    continue_on_failure: false,
    model: null,
    output_parameter: output("search_step"),
    url: "",
    navigation_goal: "Search for apple stock and get its price",
    parameters: [],
  } as unknown as WorkflowBlock;

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
        unobserve() {}
        disconnect() {}
      },
    );
    Element.prototype.scrollIntoView = vi.fn();
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  test("offers to turn into a Browser Task before running, keeping the text", () => {
    const apply = vi.fn();
    renderDoc([actionStep], {
      apply,
      update: vi.fn(),
      addStep: vi.fn(() => null),
      duplicate: vi.fn(),
    });

    fireEvent.click(
      screen.getByRole("button", { name: "Turn into Browser Task" }),
    );

    const turned = apply.mock.calls[0]![0].nodes.find(
      (node: { data: { label?: string } }) => node.data.label === "search_step",
    );
    expect(turned.type).toBe("navigation");
    expect(turned.data.navigationGoal).toBe(
      "Search for apple stock and get its price",
    );
  });
});

describe("Doc inputs", () => {
  beforeEach(() => {
    docRun.current = { runId: null, blocksByLabel: new Map(), live: false };
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
    useWorkflowParametersStore.setState({
      parameters: [
        {
          key: "query",
          parameterType: "workflow",
          dataType: WorkflowParameterValueType.String,
          description: null,
          defaultValue: "cats",
        },
      ],
    });
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  test("an input added, renamed and deleted in the Doc is the workflow's parameter and its mentions follow", () => {
    let graph = getElements(
      [
        block("text_prompt", "summarize", "each_result", {
          prompt: "Search for {{ query }}",
          parameters: [{ key: "query" }],
        }),
        forLoop("each_result", null, [code("open_result", null)], {
          loop_over: { key: "query" },
          loop_variable_reference: "query",
        }),
      ],
      settings(),
      true,
    ) as DocGraph;
    const client = new QueryClient();
    const edits: DocGraphEdits = {
      apply: () => {},
      update: (change) => {
        graph = { ...graph, nodes: change(graph.nodes) };
      },
      addStep: () => null,
      duplicate: () => {},
    };
    const view = () => (
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <ReactFlowProvider>
            <WorkflowDocEditor {...graph} edits={edits} />
          </ReactFlowProvider>
        </MemoryRouter>
      </QueryClientProvider>
    );
    const rendered = render(view());
    const inPopover = (name: string) =>
      within(screen.getByRole("dialog")).getByRole("button", { name });
    const keys = () =>
      useWorkflowParametersStore.getState().parameters.map((p) => p.key);
    const loopSource = () =>
      (
        graph.nodes.find((n) => n.type === "loop")!.data as {
          loopValue: string;
        }
      ).loopValue;
    const prompt = () =>
      graph.nodes.find((n) => n.data.label === "summarize")!.data as {
        prompt: string;
        parameterKeys: string[] | null;
      };

    useWorkflowHasChangesStore.getState().setHasChanges(false);
    fireEvent.click(screen.getByRole("button", { name: "+ Input" }));
    expect(keys()).toContain("new_input");
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    // Closing an untouched new input leaves the agent as it was.
    expect(keys()).toEqual(["query"]);
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "+ Input" }));
    // A just-created placeholder is still an Add: not titled "Edit", and no "changing the type" warning.
    expect(screen.getByText("Add Input")).toBeTruthy();
    expect(screen.getByRole("dialog").getAttribute("aria-label")).toBe(
      "Add input",
    );
    fireEvent.change(
      within(screen.getByRole("dialog")).getAllByRole("textbox")[0]!,
      {
        target: { value: "topic" },
      },
    );
    fireEvent.click(inPopover("Save"));
    expect(useWorkflowParametersStore.getState().parameters[1]).toEqual({
      key: "topic",
      parameterType: "workflow",
      dataType: WorkflowParameterValueType.String,
      description: "",
      defaultValue: null,
    });
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: /^Input query/ }));
    fireEvent.change(
      within(screen.getByRole("dialog")).getAllByRole("textbox")[0]!,
      {
        target: { value: "search" },
      },
    );
    fireEvent.click(inPopover("Save"));
    expect(keys()).toEqual(["search", "topic"]);
    expect(prompt().prompt).toBe("Search for {{ search }}");
    expect(prompt().parameterKeys).toEqual(["search"]);
    expect(loopSource()).toBe("search");

    fireEvent.click(screen.getByRole("button", { name: "+ Context input" }));
    expect(screen.getByText("Add Context Input")).toBeTruthy();
    fireEvent.change(
      within(screen.getByRole("dialog")).getAllByRole("textbox")[0]!,
      { target: { value: "derived" } },
    );
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("combobox"));
    fireEvent.click(screen.getByRole("option", { name: "search" }));
    fireEvent.click(inPopover("Save"));
    expect(keys()).toEqual(["search", "topic", "derived"]);
    expect(screen.getByRole("button", { name: /^Input derived/ })).toBeTruthy();

    rendered.rerender(view());
    fireEvent.click(screen.getByRole("button", { name: /^Input search/ }));
    fireEvent.click(inPopover("Delete input"));
    expect(keys()).toEqual(["search", "topic", "derived"]);
    fireEvent.click(screen.getByRole("button", { name: /^Input derived/ }));
    fireEvent.click(inPopover("Delete input"));
    fireEvent.click(screen.getByRole("button", { name: /^Input search/ }));
    fireEvent.click(inPopover("Delete input"));
    expect(keys()).toEqual(["topic"]);
    expect(prompt().prompt).toBe("Search for {{ search }}");
    expect(prompt().parameterKeys).toEqual([]);
    rendered.rerender(view());
    expect(screen.getByTitle("{{ search }}: missing reference")).toBeTruthy();
  });
});
