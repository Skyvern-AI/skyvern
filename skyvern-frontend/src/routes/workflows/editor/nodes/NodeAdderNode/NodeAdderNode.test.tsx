// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import type { ReactNode } from "react";
import type { Edge, NodeProps } from "@xyflow/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useCopilotActionStore } from "@/store/useCopilotActionStore";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import {
  createYamlCommitOwner,
  registerEditorOwner,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";
import { DebugStoreContext } from "@/store/DebugStoreContext";
import { useSettingsStore } from "@/store/SettingsStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { useWorkflowSettingsStore } from "@/store/WorkflowSettingsStore";

import { NodeAdderNode } from "./NodeAdderNode";
import type { NodeAdderNode as NodeAdderNodeType } from "./types";

const useEdgesMock = vi.fn();
const useNodesMock = vi.fn();

vi.mock("@xyflow/react", async () => {
  const actual =
    await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");

  return {
    ...actual,
    Handle: () => null,
    useEdges: () => useEdgesMock(),
    useNodes: () => useNodesMock(),
  };
});

vi.mock("react-router-dom", async () => {
  const actual =
    await vi.importActual<typeof import("react-router-dom")>(
      "react-router-dom",
    );

  return {
    ...actual,
    useParams: () => ({ workflowPermanentId: "wpid_test" }),
  };
});

vi.mock("@/components/ui/use-toast", () => ({ toast: vi.fn() }));
vi.mock("posthog-js/react", () => ({
  usePostHog: () => ({ capture: vi.fn() }),
}));

function queryWrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={new QueryClient()}>
      {children}
    </QueryClientProvider>
  );
}

const initialSettings = useSettingsStore.getState();
const initialWorkflowSettings = useWorkflowSettingsStore.getState();

function renderNodeAdder(
  props: Partial<NodeProps<NodeAdderNodeType>>,
  { debug = false }: { debug?: boolean } = {},
) {
  return render(
    <DebugStoreContext.Provider
      value={{ isDebugMode: debug, blockRunsEnabled: false }}
    >
      <NodeAdderNode
        {...({
          id: "adder",
          parentId: "loop",
          ...props,
        } as NodeProps<NodeAdderNodeType>)}
      />
    </DebugStoreContext.Provider>,
    { wrapper: queryWrapper },
  );
}

describe("NodeAdderNode", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    registerEditorOwner(createYamlCommitOwner("wpid_test"));
    useWorkflowHasChangesStore.setState(
      useWorkflowHasChangesStore.getInitialState(),
    );
    useSettingsStore.setState(initialSettings, true);
    useWorkflowSettingsStore.setState(initialWorkflowSettings, true);
    useWorkflowPanelStore.setState({
      workflowPanelState: {
        active: false,
        content: "parameters",
      },
    });

    useSettingsStore.getState().setIsUsingABrowser(false);
    useSettingsStore.getState().setIsLoadingABrowser(false);
    useWorkflowSettingsStore.setState({ finallyBlockLabel: null });
    useEdgesMock.mockReset();
    useNodesMock.mockReset();
  });

  afterEach(() => {
    cleanup();
    useCopilotActionStore.setState({ stageAttachment: null });
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
  });

  it("stages a picked PDF in Copilot with a draft naming the block before it", () => {
    useEdgesMock.mockReturnValue([
      { id: "edge", source: "login", target: "adder" },
    ]);
    useNodesMock.mockReturnValue([
      { id: "login", type: "task", data: { label: "login" } },
    ]);
    const { container } = renderNodeAdder({}, { debug: true });
    const uploadItem = () =>
      screen.getByText("Upload SOP").parentElement!.querySelector("button")!;
    expect(uploadItem().disabled).toBe(true);

    const stage = vi.fn();
    act(() => useCopilotActionStore.setState({ stageAttachment: stage }));
    expect(uploadItem().disabled).toBe(false);
    const file = new File(["pdf"], "steps.pdf", { type: "application/pdf" });
    fireEvent.change(
      container.querySelector<HTMLInputElement>('input[type="file"]')!,
      { target: { files: [file] } },
    );
    expect(stage).toHaveBeenCalledWith(
      file,
      "Add the steps from the attached SOP after the login block.",
    );
  });

  it("keeps the loop as parent without branch context when adding the first block in a nested loop", () => {
    // SKY-10719: adding a block into a loop nested in a conditional keeps the
    // loop as parent with no branch context (loop children are not branch members).
    const edges: Array<Edge> = [
      {
        id: "edge",
        source: "start",
        target: "adder",
      },
    ];

    useEdgesMock.mockReturnValue(edges);
    useNodesMock.mockReturnValue([
      {
        id: "conditional",
        type: "conditional",
        data: {
          activeBranchId: "branch-a",
          branches: [{ id: "branch-a" }],
          label: "conditional",
          mergeLabel: null,
        },
      },
      {
        id: "loop",
        parentId: "conditional",
        type: "loop",
        data: {
          conditionalBranchId: "branch-a",
          conditionalLabel: "conditional",
          conditionalMergeLabel: null,
          conditionalNodeId: "conditional",
          label: "block_8",
        },
      },
      {
        id: "start",
        parentId: "loop",
        type: "start",
        data: {},
      },
    ]);

    renderNodeAdder({});

    fireEvent.click(screen.getByTestId("node-adder-button"));

    const state = useWorkflowPanelStore.getState().workflowPanelState;
    expect(state).toMatchObject({
      active: true,
      content: "nodeLibrary",
      data: {
        next: "adder",
        parent: "loop",
        previous: "start",
      },
    });
    expect(state.data?.branchContext).toBeUndefined();
  });
});
