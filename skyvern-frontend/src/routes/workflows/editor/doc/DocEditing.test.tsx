// @vitest-environment jsdom
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { useContext } from "react";

import { useCopilotActionStore } from "@/store/useCopilotActionStore";
import { AxiosError } from "axios";

import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { useWorkflowHistoryAccessStore } from "@/store/WorkflowHistoryAccessStore";
import {
  useWorkflowYamlEditorStore,
  type YamlCommitOwner,
} from "@/store/WorkflowYamlEditorStore";
import { useWorkflowSnapshotStore } from "@/store/WorkflowSnapshotStore";

import { DocEditBar, DocStepPicker } from "./DocEditing";
import { useDocSaveFailureStore } from "./docSaveState";
import { caretOnEdge } from "./docCaret";
import { DocStepHeader } from "./DocStep";
import { WorkflowDocActionsContext } from "./WorkflowDocActionsContext";
import { createDocEditApi, TOP_SCOPE } from "./docEditApi";
import type { DocGraph } from "./docGraphEdits";
import { getElements } from "../workflowEditorUtils";
import { isWorkflowBlockNode } from "../nodes";
import { block, settings } from "./docTestBlocks";

const save = vi.hoisted(() => vi.fn(async () => undefined));
const catalogState = vi.hoisted(() => ({
  data: undefined as
    | Array<{ id: string; status: "live" | "comingSoon" }>
    | undefined,
}));
vi.mock("../hooks/useSaveWorkflow", () => ({ useSaveWorkflow: () => save }));
vi.mock("./useDocRun", () => ({
  useDocRun: () => ({
    runId: undefined,
    run: undefined,
    blocksByLabel: new Map(),
    loopsByLabel: new Map(),
    live: false,
    blockRun: false,
    unknownLabels: new Set(),
  }),
}));
vi.mock("@/hooks/useIntegrationCatalogQuery", () => ({
  useIntegrationCatalogQuery: () => ({ data: catalogState.data }),
}));

afterEach(() => {
  cleanup();
  save.mockClear();
  catalogState.data = undefined;
  useCopilotActionStore.setState({ pendingGoalChanges: [] });
  useWorkflowHistoryAccessStore.getState().reset();
  useWorkflowSnapshotStore.getState().clearSnapshot();
  useWorkflowHasChangesStore.setState({ hasChanges: false });
  useWorkflowYamlEditorStore.setState({ editorOwner: null, pendingSaves: {} });
  useDocSaveFailureStore.setState({ failures: {} });
});

describe("Doc edit bar row", () => {
  test("an idle bar reserves its phone row only when it floats", () => {
    const { container, rerender } = render(<DocEditBar floating />);
    expect(container.querySelector('[aria-hidden="true"]')).toBeTruthy();
    rerender(<DocEditBar />);
    expect(container.querySelector('[aria-hidden="true"]')).toBe(null);
  });
});

describe("Doc Save", () => {
  test("asks about a new Goal that isn't applied before saving", () => {
    useWorkflowHasChangesStore.setState({ hasChanges: true });
    useCopilotActionStore.setState({
      pendingGoalChanges: [
        { label: "read_total", goal: "Read the total", previousGoal: "Old" },
      ],
    });
    render(<DocEditBar />);

    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(screen.getByText("New Goal not applied")).toBeTruthy();
    expect(save).not.toHaveBeenCalled();
  });

  test("saves straight away when no Goal is pending", () => {
    useWorkflowHasChangesStore.setState({ hasChanges: true });
    render(<DocEditBar />);

    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(save).toHaveBeenCalledTimes(1);
  });

  test("Cmd+S saves the Doc and keeps the browser's own save out", () => {
    useWorkflowHasChangesStore.setState({ hasChanges: true });
    render(<DocEditBar />);

    const proceeded = fireEvent.keyDown(document.body, {
      key: "s",
      metaKey: true,
    });

    expect(proceeded).toBe(false);
    expect(save).toHaveBeenCalledTimes(1);
  });

  test("a failed save says why, keeps the edit and offers Try again", async () => {
    useWorkflowYamlEditorStore.setState({
      editorOwner: { workflowPermanentId: "wpid_1", active: true },
    });
    useWorkflowHasChangesStore.setState({ hasChanges: true });
    save.mockRejectedValueOnce(new AxiosError("Network Error", "ERR_NETWORK"));
    render(<DocEditBar />);

    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(
      await screen.findByText(/Couldn't save: can't reach the server/),
    ).toBeTruthy();
    expect(screen.queryByText("Unsaved changes")).toBe(null);
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(save).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByText(/Couldn't save/)).toBe(null));
  });

  test("a failed save releases its own workflow's lock, not the one now open", async () => {
    const a: YamlCommitOwner = { workflowPermanentId: "wpid_a", active: true };
    const b: YamlCommitOwner = { workflowPermanentId: "wpid_b", active: true };
    const pending = (owner: YamlCommitOwner) => ({
      owner,
      kind: "save" as const,
      persisting: true,
    });
    useWorkflowYamlEditorStore.setState({
      editorOwner: a,
      pendingSaves: { wpid_a: pending(a), wpid_b: pending(b) },
    });
    useWorkflowHasChangesStore.setState({ hasChanges: true });
    save.mockImplementationOnce(async () => {
      useWorkflowYamlEditorStore.setState({ editorOwner: b });
      throw new AxiosError("Network Error", "ERR_NETWORK");
    });
    render(<DocEditBar />);

    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(
        useWorkflowYamlEditorStore.getState().pendingSaves.wpid_a,
      ).toBeUndefined(),
    );
    expect(
      useWorkflowYamlEditorStore.getState().pendingSaves.wpid_b?.persisting,
    ).toBe(true);
    // The reason belongs to the workflow that failed, not to the one now open.
    expect(useDocSaveFailureStore.getState().failures).toEqual({
      wpid_a: "can't reach the server",
    });
    expect(screen.queryByText(/Couldn't save/)).toBe(null);
  });

  test("a failure recorded for another workflow does not show on this bar", () => {
    useWorkflowYamlEditorStore.setState({
      editorOwner: { workflowPermanentId: "wpid_b", active: true },
    });
    useDocSaveFailureStore.setState({
      failures: { wpid_a: "can't reach the server" },
    });
    useWorkflowHasChangesStore.setState({ hasChanges: true });
    render(<DocEditBar />);

    expect(screen.queryByText(/Couldn't save/)).toBe(null);
    expect(screen.getByRole("button", { name: "Save" })).toBeTruthy();
  });

  test("the bar drops only its own workflow's failure when the Doc unmounts or has no unsaved changes", () => {
    useWorkflowYamlEditorStore.setState({
      editorOwner: { workflowPermanentId: "wpid_1", active: true },
    });
    const failures = {
      wpid_1: "can't reach the server",
      wpid_2: "the server had a problem",
    };
    useDocSaveFailureStore.setState({ failures });
    useWorkflowHasChangesStore.setState({ hasChanges: true });
    const { unmount } = render(<DocEditBar />);
    expect(
      screen.getByText(/Couldn't save: can't reach the server/),
    ).toBeTruthy();
    unmount();
    expect(useDocSaveFailureStore.getState().failures).toEqual({
      wpid_2: "the server had a problem",
    });

    useDocSaveFailureStore.setState({ failures });
    render(<DocEditBar />);
    act(() => useWorkflowHasChangesStore.getState().setHasChanges(false));
    expect(useDocSaveFailureStore.getState().failures).toEqual({
      wpid_2: "the server had a problem",
    });
  });

  test("says Saved, not Unsaved, once the doc is back at the saved version", async () => {
    useWorkflowHasChangesStore.setState({ hasChanges: true });
    useWorkflowSnapshotStore.setState({
      snapshot: {} as never,
      userHasEdited: true,
      contentDirty: false,
    });
    useWorkflowHistoryAccessStore.setState({ canRedo: true });
    render(<DocEditBar floating />);

    await waitFor(() => expect(screen.getByText("Saved")).toBeTruthy());
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
    expect(screen.queryByText("Unsaved changes")).toBe(null);
  });
});

describe("Add a step search", () => {
  beforeEach(() => {
    globalThis.ResizeObserver ??= class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
    Element.prototype.scrollIntoView ??= () => {};
  });

  test("Turn into applies the new type and closes the action menu and picker", async () => {
    const graph = getElements(
      [
        block("text_prompt", "draft", null, {
          prompt: "Draft a summary",
          parameters: [],
        }),
      ],
      settings(),
      true,
    ) as DocGraph;
    const node = graph.nodes.find(isWorkflowBlockNode)!;
    let applied = graph;
    const edit = createDocEditApi({
      ...graph,
      edits: {
        apply: (next) => {
          applied = next;
        },
        update: () => {},
        addStep: () => null,
        duplicate: () => {},
      },
      finallyId: null,
      workflowId: "wpid_test",
      selectBranch: () => {},
      selectedBranch: () => undefined,
      inputKeys: [],
      credentialKeys: new Set(),
    });
    function Header() {
      const actions = useContext(WorkflowDocActionsContext);
      return (
        <WorkflowDocActionsContext.Provider
          value={{ ...actions, editable: true, edit }}
        >
          <DocStepHeader
            node={node}
            editable
            showRunControl={false}
            place={{ scope: TOP_SCOPE, index: 0, count: 1, isFinally: false }}
          />
        </WorkflowDocActionsContext.Provider>
      );
    }
    render(<Header />);
    const tag = screen.getByRole("button", { name: /step draft, actions/ });
    fireEvent.click(tag);
    const mainMenu = screen.getByRole("menu");
    fireEvent.keyDown(screen.getByRole("menuitem", { name: "Turn into…" }), {
      key: "ArrowRight",
    });
    const wait = await screen.findByRole("option", { name: /^Wait/ });
    expect(mainMenu.contains(wait)).toBe(false);
    fireEvent.click(wait);

    await waitFor(() =>
      expect(
        applied.nodes.find((candidate) => candidate.id === node.id)?.type,
      ).toBe("wait"),
    );
    expect(tag.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByPlaceholderText("Turn into…")).toBeNull();
  });

  test("Power BI is offered only when the served catalog marks it live", () => {
    const { rerender } = render(<DocStepPicker onPick={() => {}} />);
    expect(screen.queryByRole("option", { name: /Power BI Query/ })).toBeNull();
    catalogState.data = [{ id: "powerbi", status: "comingSoon" }];
    rerender(<DocStepPicker onPick={() => {}} />);
    expect(screen.queryByRole("option", { name: /Power BI Query/ })).toBeNull();
    catalogState.data = [{ id: "powerbi", status: "live" }];
    rerender(<DocStepPicker onPick={() => {}} />);
    expect(screen.getByRole("option", { name: /Power BI Query/ })).toBeTruthy();
  });

  test("an exact step name comes first, above steps that only mention it", () => {
    render(<DocStepPicker onPick={() => {}} />);
    fireEvent.change(screen.getByPlaceholderText("Add a step…"), {
      target: { value: "Wait" },
    });
    const options = screen.getAllByRole("option");
    expect(options[0]!.textContent).toMatch(/^Wait/);
    expect(options[0]!.getAttribute("aria-selected")).toBe("true");
  });

  test("the words the Doc shows put Conditional and Loop first, even when another group mentions them", () => {
    render(<DocStepPicker onPick={() => {}} />);
    const input = screen.getByPlaceholderText("Add a step…");
    fireEvent.change(input, { target: { value: "If" } });
    expect(screen.getAllByRole("option")[0]!.textContent).toMatch(
      /^Conditional/,
    );
    fireEvent.change(input, { target: { value: "For" } });
    expect(screen.getAllByRole("option")[0]!.textContent).toMatch(/^Loop/);
  });
});

describe("caretOnEdge", () => {
  test("a caret with no box in the middle of wrapped text is not on an edge", () => {
    const rect = (top: number, height: number) =>
      ({ top, bottom: top + height, height }) as DOMRect;
    const el = document.createElement("div");
    el.contentEditable = "true";
    el.textContent = "first wrapped line second wrapped line";
    el.getBoundingClientRect = () => rect(0, 40);
    document.body.appendChild(el);
    const proto = Range.prototype as unknown as Record<string, unknown>;
    const saved = [proto.getClientRects, proto.getBoundingClientRect];
    // jsdom has no layout: a collapsed caret has no box, and the one character next to it sits on the second line.
    proto.getBoundingClientRect = () => rect(0, 0);
    proto.getClientRects = function (this: Range) {
      return this.startOffset === 25 && this.endOffset === 26
        ? [rect(20, 20)]
        : [];
    };
    try {
      const text = el.firstChild as Text;
      const range = document.createRange();
      range.setStart(text, 25);
      range.collapse(true);
      window.getSelection()!.removeAllRanges();
      window.getSelection()!.addRange(range);
      expect(caretOnEdge(el, "first")).toBe(false);
      expect(caretOnEdge(el, "last")).toBe(true);
    } finally {
      [proto.getClientRects, proto.getBoundingClientRect] = saved;
      el.remove();
    }
  });
});
