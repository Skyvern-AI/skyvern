// @vitest-environment jsdom
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { useContext } from "react";

import { useCopilotActionStore } from "@/store/useCopilotActionStore";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";

import { DocEditBar, DocStepPicker } from "./DocEditing";
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
vi.mock("@/hooks/useIntegrationCatalogQuery", () => ({
  useIntegrationCatalogQuery: () => ({ data: catalogState.data }),
}));

afterEach(() => {
  cleanup();
  save.mockClear();
  catalogState.data = undefined;
  useCopilotActionStore.setState({ pendingGoalChanges: [] });
});

describe("Doc Save", () => {
  test("asks about a new Goal that isn't applied before saving", () => {
    useWorkflowHasChangesStore.setState({ hasChanges: true });
    useCopilotActionStore.setState({
      pendingGoalChanges: [
        { label: "read_total", goal: "Read the total", previousGoal: "Old" },
      ],
    });
    render(<DocEditBar problems={0} />);

    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(screen.getByText("New Goal not applied")).toBeTruthy();
    expect(save).not.toHaveBeenCalled();
  });

  test("saves straight away when no Goal is pending", () => {
    useWorkflowHasChangesStore.setState({ hasChanges: true });
    render(<DocEditBar problems={0} />);

    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(save).toHaveBeenCalledTimes(1);
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
});
